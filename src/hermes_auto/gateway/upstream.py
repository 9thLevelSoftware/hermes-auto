"""OpenAI-compatible candidate clients and the reusable client pool.

Four properties are load-bearing.

**The read timeout is unbounded.** A model generating for four minutes is not a
stalled connection, and any finite read timeout eventually cuts a long generation
mid-stream and reports it as a transport error. Connect, write, and pool
timeouts stay finite, because those failures are real.

**``Accept-Encoding: identity`` is forced on the upstream client.** The response
relay iterates *undecoded* bytes, so a compressing upstream would put gzip on the
wire to the client. That is survivable only if ``Content-Encoding`` survives with
it, and the local mock never compresses, so no test in this phase could observe
the mismatch. Removing the possibility is cheaper than testing for it.

**Status and headers are returned alongside the byte iterator, not inside it.**
An async generator cannot hand back a status code before its first yield, and
several required behaviors -- relaying a 429 with its ``Retry-After``, relaying a
400 body unchanged -- need the status before the first byte is read. So
:meth:`UpstreamClient.stream` is an ordinary coroutine that opens the response,
then returns ``(status, headers, iterator)``.

**The credential is resolved from the environment and never leaves this module.**
It is read once at construction from the variable named by ``credential_ref``,
stored on the instance, and written only into an outbound ``Authorization``
header. It is never logged, never echoed into an error message, and never placed
in an exception.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

import httpx

from ..config import AutoRouterConfig, CandidateConfig, UpstreamConfig
from ..telemetry.redaction import sanitize_url
from .errors import GatewayError

__all__ = [
    "HOP_BY_HOP_HEADERS",
    "REQUEST_DROP_HEADERS",
    "RESPONSE_DROP_HEADERS",
    "UPSTREAM_TIMEOUT",
    "UpstreamClient",
    "UpstreamClientPool",
    "UpstreamConfigError",
    "filter_request_headers",
    "filter_response_headers",
]


#: RFC 9110 connection-specific header fields. These describe a single hop and
#: are meaningless -- or actively wrong -- when copied onto the next one. Copying
#: ``Transfer-Encoding`` in particular produces a response framed twice, which
#: some clients read as a truncated body and others as a protocol error.
HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
    }
)

#: Additionally dropped when forwarding a client request upstream.
#:
#: ``authorization`` is dropped because the inbound value is *this gateway's*
#: bearer token; forwarding it would hand the local token to a third-party
#: provider. The upstream credential replaces it. ``host`` and ``content-length``
#: are recomputed by httpx for the new request, and a stale copy of either
#: desynchronizes the connection. ``accept-encoding`` is replaced by the client's
#: forced ``identity``.
REQUEST_DROP_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS | {
    "authorization",
    "host",
    "content-length",
    "accept-encoding",
}

#: Dropped when relaying an upstream response back to the client. Hop-by-hop
#: only -- the set is deliberately no larger than that.
#:
#: ``content-length`` is **kept**. A chunked upstream does not send one, so the
#: header is present only when the upstream framed by length, and in that case
#: the relayed bytes are exactly that many: ``aiter_raw`` yields the transport
#: body with chunked framing already removed and no content transformation
#: applied. Keeping it preserves a header the caller would have seen talking to
#: the provider directly, which is the whole of R5. It also makes a truncated
#: relay surface to the client as the protocol error it is, rather than as a
#: clean short body.
#:
#: ``content-encoding`` is likewise kept. The client forces
#: ``Accept-Encoding: identity``, so an upstream should not compress; if one
#: does anyway, the bytes relayed are the compressed ones and the header
#: describing them must travel with them. Filtering it here is how a relay ends
#: up handing a client gzip labelled as plain text -- and the local mock never
#: compresses, so no test in this phase would catch it.
RESPONSE_DROP_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS

#: Finite where a stall is a real failure; unbounded where it is not.
UPSTREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)


class UpstreamConfigError(Exception):
    """The upstream target cannot be used as configured.

    Raised at construction, not per request: a missing credential is an
    installation problem, and discovering it on the first user prompt rather than
    at startup wastes the operator's time and the user's.
    """


def _filter(
    headers: Iterable[tuple[str, str]], drop: frozenset[str]
) -> list[tuple[str, str]]:
    """Return *headers* minus *drop*, preserving order and duplicates.

    Order and duplicates are preserved rather than collapsed into a dict because
    a response may legitimately carry repeated headers, and collapsing them
    silently discards all but one.
    """
    return [(name, value) for name, value in headers if name.lower() not in drop]


def filter_request_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Headers to forward from the client's request to the upstream."""
    return _filter(headers, REQUEST_DROP_HEADERS)


def filter_response_headers(
    headers: Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Headers to relay from the upstream's response back to the client."""
    return _filter(headers, RESPONSE_DROP_HEADERS)


def _resolve_credential(upstream: UpstreamConfig | CandidateConfig) -> str | None:
    """Read the credential named by ``credential_ref``, or None for ``none``.

    Raises ``UpstreamConfigError`` naming the *variable*, never its value, when
    the reference points at an environment variable that is unset or empty.
    """
    variable = upstream.credential_env_var
    if variable is None:
        return None
    value = os.environ.get(variable)
    if not value:
        raise UpstreamConfigError(
            f"upstream.credential_ref names environment variable {variable!r}, "
            f"which is unset or empty. Set it in the environment -- credentials "
            f"never belong in config.yaml."
        )
    return value


class UpstreamClient:
    """One ``httpx.AsyncClient`` against one configured OpenAI-compatible target.

    Built once in the app's lifespan startup and closed on shutdown. Building one
    per request would discard connection reuse, and re-establishing a TLS session
    on every turn is a TTFT regression that shows up as "the router feels slow"
    rather than as a test failure.
    """

    def __init__(
        self,
        config: AutoRouterConfig | UpstreamConfig | CandidateConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        upstream = config.upstream if isinstance(config, AutoRouterConfig) else config
        self.config = upstream
        self._credential = _resolve_credential(upstream)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=upstream.base_url.rstrip("/"),
            timeout=UPSTREAM_TIMEOUT,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        )

    # -- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying connection pool. Idempotent."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> UpstreamClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- request construction ---------------------------------------------

    def _build(
        self, path: str, body: bytes, headers: Iterable[tuple[str, str]]
    ) -> httpx.Request:
        outbound = filter_request_headers(headers)
        outbound.append(("content-type", "application/json"))
        # Force identity even if the caller sent something else: the relay
        # iterates undecoded bytes. Set after filtering so it cannot be shadowed.
        outbound.append(("accept-encoding", "identity"))
        if self._credential is not None:
            outbound.append(("authorization", f"Bearer {self._credential}"))
        # Any duplicate of a header name we appended above wins by being last,
        # which is what httpx's Headers does for a repeated single-value field.
        deduped: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name, value in reversed(outbound):
            lowered = name.lower()
            if lowered in {"content-type", "accept-encoding", "authorization"}:
                if lowered in seen:
                    continue
                seen.add(lowered)
            deduped.append((name, value))
        deduped.reverse()
        return self._client.build_request(
            "POST", path, content=body, headers=deduped
        )

    # -- the two verbs -----------------------------------------------------

    async def stream(
        self,
        body: bytes,
        headers: Iterable[tuple[str, str]] | Mapping[str, str] | None = None,
        *,
        path: str = "/chat/completions",
    ) -> tuple[int, httpx.Headers, AsyncIterator[bytes]]:
        """Open a streaming request and return status, headers, and raw bytes.

        The third element is an iterator over ``aiter_raw()`` -- the undecoded
        transport bytes, with no frame reassembly of any kind. It owns closing
        the response, including when the consumer is cancelled part-way through,
        so a client that hangs up mid-generation releases the upstream connection
        rather than leaving it billing.

        The caller must consume or close the iterator. Consuming it to
        completion, closing it, or having it cancelled all release the response.
        """
        request = self._build(path, body, _as_pairs(headers))
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise _connect_error(exc, self.config.base_url) from exc

        async def _iterate() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                # Runs on completion, on GeneratorExit from an early close, and
                # on CancelledError when the client disconnects mid-stream.
                await response.aclose()

        return response.status_code, response.headers, _iterate()

    async def complete(
        self,
        body: bytes,
        headers: Iterable[tuple[str, str]] | Mapping[str, str] | None = None,
        *,
        path: str = "/chat/completions",
    ) -> tuple[int, httpx.Headers, bytes]:
        """Perform a non-streaming request and return status, headers, and body.

        Returns the body verbatim. A non-2xx is *returned*, not raised: the
        gateway relays a provider's error rather than reinterpreting it.
        """
        request = self._build(path, body, _as_pairs(headers))
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise _connect_error(exc, self.config.base_url) from exc
        # ``stream=True`` plus ``aiter_raw`` rather than ``response.content``:
        # ``.content`` is *decoded*, while ``stream()`` relays raw bytes, and both
        # verbs share one RESPONSE_DROP_HEADERS policy that keeps
        # ``content-encoding`` and ``content-length``. A compressing upstream
        # therefore put decoded bytes under a compressed length and the response
        # aborted. Reading raw here keeps one header policy correct for both.
        try:
            chunks: list[bytes] = []
            async for chunk in response.aiter_raw():
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise _connect_error(exc, self.config.base_url) from exc
        finally:
            await response.aclose()
        return response.status_code, response.headers, b"".join(chunks)

    async def reachable(self, *, timeout: float = 2.0) -> Any:
        """Delegate to :mod:`hermes_auto.health.probe`; return a ProbeResult."""
        from ..health.probe import check_upstream_reachable

        return await check_upstream_reachable(self.config.base_url, timeout=timeout)


class UpstreamClientPool:
    """Lazily constructed, reusable clients keyed by candidate id."""

    def __init__(
        self,
        config: AutoRouterConfig,
        *,
        client_factory: Any = None,
    ) -> None:
        self.candidates = config.resolved_candidates
        self._client_factory = client_factory or UpstreamClient
        self._clients: dict[str, UpstreamClient] = {}

    def _get(self, candidate: CandidateConfig) -> UpstreamClient:
        client = self._clients.get(candidate.id)
        if client is None:
            client = self._client_factory(candidate)
            self._clients[candidate.id] = client
        return client

    async def stream(
        self,
        candidate: CandidateConfig,
        body: bytes,
        headers: Iterable[tuple[str, str]] | Mapping[str, str] | None = None,
    ) -> tuple[int, httpx.Headers, AsyncIterator[bytes]]:
        return await self._get(candidate).stream(body, headers)

    async def complete(
        self,
        candidate: CandidateConfig,
        body: bytes,
        headers: Iterable[tuple[str, str]] | Mapping[str, str] | None = None,
    ) -> tuple[int, httpx.Headers, bytes]:
        return await self._get(candidate).complete(body, headers)

    async def reachable(self, *, timeout: float = 2.0) -> Any:
        last: Any = None
        for candidate in self.candidates:
            try:
                result = await self._get(candidate).reachable(timeout=timeout)
            except UpstreamConfigError:
                continue
            last = result
            if result.ready:
                return result
        if last is not None:
            return last
        from ..health.probe import ProbeResult

        return ProbeResult(False, "no candidate has an available credential")

    async def aclose(self) -> None:
        for client in tuple(self._clients.values()):
            await client.aclose()
        self._clients.clear()


def _as_pairs(
    headers: Iterable[tuple[str, str]] | Mapping[str, str] | None,
) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if isinstance(headers, Mapping):
        return list(headers.items())
    return list(headers)


def _connect_error(exc: httpx.HTTPError, base_url: str) -> GatewayError:
    """Map a transport failure to a 502 in the error envelope.

    502 rather than 500: the gateway is working, the thing behind it is not, and
    a client retry policy keyed on 5xx-versus-502 should be able to tell those
    apart. The message names the exception *type*, never its string, which can
    contain a full URL with an embedded credential.

    It also names the configured URL -- **sanitized**. Being operator-supplied
    configuration does not make it non-secret: ``https://user:pw@host/v1`` is a
    legal URL, and this body goes to the *client*, who is not necessarily the
    operator who wrote the config. Host and port survive so the message still
    says which endpoint was unreachable.
    """
    return GatewayError(
        502,
        f"upstream at {sanitize_url(base_url)} is unreachable ({type(exc).__name__})",
        "api_error",
        code="upstream_unavailable",
    )
