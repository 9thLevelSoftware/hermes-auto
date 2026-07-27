"""The inference ASGI app: four routes, one upstream, no routing.

``create_app()`` is pure. It builds routes and returns a ``Starlette`` instance
and does nothing else -- no socket, no HTTP client, no schema compilation, no
token read, no directory creation. Everything with a cost or a side effect
happens in the lifespan handler, which runs when a server actually starts the
app. That split is what lets a verification command, a test, and a documentation
generator all call ``create_app()`` without a live upstream or a state directory.

**Starlette 1.3.1, not 0.3x.** ``Starlette.__init__`` in this version takes
``debug``, ``routes``, ``middleware``, ``exception_handlers`` and ``lifespan``
and **no** ``on_startup``/``on_shutdown``; those parameters were removed, and
passing them is a ``TypeError``, not a deprecation warning. ``Router`` also warns
on a bare async-generator lifespan, so the handler below is an explicit
``@asynccontextmanager``. See this plan's report for the full delta.

**Health and readiness are separate questions.** ``/healthz`` is liveness and
answers from process-local state only. ``/readyz`` reaches out to the upstream.
Aliasing them makes supervision restart the gateway whenever the provider has a
bad minute -- see :mod:`hermes_auto.health.probe`.

**What ``/healthz`` returns for ``instance_id``, and why it is not read from the
runtime file.** Plan 02-07 defeats PID reuse by comparing the ``instance_id`` in
``<state_dir>/runtime/gateway.json`` against the one ``/healthz`` reports on the
recorded port: match means running, connection refused means stale, and a
*mismatch* means some other process now owns that port. That last case only
exists if ``/healthz`` reports the id of **this process**. A ``/healthz`` that
read the file would return the file's own value to every caller, making mismatch
unreachable and the defense inert. So the id is minted once per process, held in
``app.state``, and written to the runtime file by ``main.py`` -- equal by
construction when healthy, and unequal in exactly the case the check exists for.

**Logging.** Every record goes through ``telemetry/redaction.py``'s ``get_logger``.
Nothing here logs message content, tool arguments, or a raw session id. The
envelope's ``root_session_id`` is passed under its own key so the sink replaces
it with a salted ``root_session_hash``; it is never formatted into a message
string, which would defeat the sink entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ..config import AutoRouterConfig, load_config
from ..state.paths import state_dir as resolve_state_dir
from ..state.runtime import new_instance_id
from ..telemetry.redaction import get_logger
from . import ingress
from .auth import mint_token, read_token
from .errors import (
    GatewayError,
    error_response,
    gateway_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
)
from .relay import relay_stream
from .upstream import UpstreamClient, filter_response_headers

__all__ = ["VIRTUAL_MODELS", "create_app"]

#: The virtual model ids the provider plugin (R1) registers. ``/v1/models``
#: reports these and does **not** proxy the upstream's catalogue: Hermes calls
#: this endpoint to populate its model picker, and the picker must offer the
#: router's four lanes, not the single fixed target behind them.
VIRTUAL_MODELS: tuple[str, ...] = (
    "auto:quality",
    "auto:balanced",
    "auto:economy",
    "auto:session",
)

#: ``created`` for every virtual model. A fixed constant rather than
#: ``time.time()``: two calls to ``/v1/models`` must return byte-identical
#: bodies, and a clock-derived field makes every response differ from the last
#: for no informational gain. 2025-01-01T00:00:00Z.
VIRTUAL_MODEL_CREATED = 1735689600

_OWNER = "hermes-auto-router"


def _client_headers(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """The inbound headers, as ordered pairs, for the upstream filter."""
    raw = getattr(headers, "raw", None)
    if raw is not None:
        return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in raw]
    return list(headers.items())


def _relayed(headers: Any) -> list[tuple[bytes, bytes]]:
    """Upstream response headers, filtered, in Starlette's raw wire form.

    Built as raw byte pairs rather than a dict so order and duplicates survive.
    A dict would collapse repeated headers to one, silently discarding the rest.
    """
    items = headers.multi_items() if hasattr(headers, "multi_items") else headers.items()
    return [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in filter_response_headers(list(items))
    ]


async def _release(*iterators: Any) -> None:
    """Close each async iterator in turn, swallowing anything they raise.

    Runs detached from the request, so an exception here has nowhere useful to
    go and must not become an unretrieved-task warning in the log.
    """
    for iterator in iterators:
        closer = getattr(iterator, "aclose", None)
        if closer is None:
            continue
        with contextlib.suppress(Exception):
            await closer()


class _RelayResponse(StreamingResponse):
    """``StreamingResponse`` that reliably releases the upstream connection.

    This closes a real gap rather than adding polish. Starlette's
    ``StreamingResponse`` never calls ``aclose()`` on its ``body_iterator``, and
    the relay chain is two nested async generators over a live ``httpx``
    response. When a client hangs up, one of two things is true:

    * The generators are suspended inside an ``await`` on the socket. The
      ``CancelledError`` lands there, both ``finally`` blocks run, and the
      upstream response is closed. Fine.
    * The generators are suspended at a ``yield``, waiting to be pulled -- which
      is the common case while data is actively flowing. **No exception is
      delivered to them at all.** The task dies, the generators are merely
      dropped, and the upstream connection stays open until the garbage
      collector happens to reach them and the loop's async-generator finalizer
      happens to run. On a paid endpoint that is an abandoned generation still
      being billed, and it is not deterministic enough to test.

    So the close is explicit, and it is scheduled with ``create_task`` rather
    than awaited. ``create_task`` is synchronous and therefore cannot itself be
    cancelled by the cancel scope that just fired; awaiting the close here would
    be cancelled immediately and complete nothing. Closing is idempotent, so the
    ordinary completion path pays nothing for this.
    """

    def __init__(self, source: AsyncIterator[bytes], **kwargs: Any) -> None:
        self._source = source
        super().__init__(relay_stream(source), **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Relay first, then the upstream iterator underneath it: closing the
            # outer generator does not close the inner one it was iterating.
            asyncio.get_running_loop().create_task(
                _release(self.body_iterator, self._source)
            )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def chat_completions(request: Request) -> Response:
    """``POST /v1/chat/completions`` -- authenticate, strip, forward, relay."""
    state = request.app.state

    ingress.authenticate(request, state.token)
    raw = await ingress.enforce_limits(request, state.max_body_bytes)

    body = ingress.parse_body(raw)
    forwarded, envelope = ingress.strip_envelope(body)
    ingress.validate_envelope(envelope)
    ingress.validate_request(forwarded, state.strict_validation)

    payload = ingress.encode_body(forwarded)
    outbound = _client_headers(request.headers)

    # `_hermes_auto` is gone from `forwarded` by construction: strip_envelope
    # popped it and `payload` is a re-dump of what is left. There is no code path
    # from here to the upstream that could put it back.
    if forwarded.get("stream") is True:
        status, headers, chunks = await state.upstream.stream(payload, outbound)
        state.logger.info(
            {
                "event": "relay.stream.opened",
                "status": status,
                "virtual_model": envelope.get("virtual_model"),
                "root_session_id": envelope.get("root_session_id"),
                "request_bytes": len(payload),
            }
        )
        response = _RelayResponse(chunks, status_code=status)
        response.raw_headers = _relayed(headers)
        return response

    status, headers, content = await state.upstream.complete(payload, outbound)
    state.logger.info(
        {
            "event": "relay.complete",
            "status": status,
            "virtual_model": envelope.get("virtual_model"),
            "root_session_id": envelope.get("root_session_id"),
            "request_bytes": len(payload),
            "response_bytes": len(content),
        }
    )
    response = Response(content, status_code=status)
    relayed = _relayed(headers)
    if not any(name == b"content-length" for name, _ in relayed):
        relayed.append((b"content-length", str(len(content)).encode("latin-1")))
    response.raw_headers = relayed
    return response


async def list_models(request: Request) -> Response:
    """``GET /v1/models`` -- the four virtual lanes, in OpenAI list shape."""
    ingress.authenticate(request, request.app.state.token)
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": VIRTUAL_MODEL_CREATED,
                    "owned_by": _OWNER,
                }
                for model_id in VIRTUAL_MODELS
            ],
        }
    )


async def healthz(request: Request) -> Response:
    """``GET /healthz`` -- liveness, plus the identity supervision compares.

    Unauthenticated and deliberately so: plan 02-07's stale-file check probes
    this on a recorded port precisely when it does not yet know whether the
    process behind that port is ours. Loopback binding is the access control, and
    the body carries no secret -- ``instance_id`` is a random per-start token
    whose only use is being compared against the runtime file.

    Answers from process-local state and touches nothing external, so it stays
    true while every upstream is down. That is the point: a restart would not fix
    a provider outage.
    """
    return JSONResponse(
        {"status": "ok", "instance_id": request.app.state.instance_id}
    )


async def readyz(request: Request) -> Response:
    """``GET /readyz`` -- readiness, which requires the upstream to answer.

    503 with a reason rather than 200-with-a-flag: a load balancer and a monitor
    both act on the status code, and a 200 body saying "not ready" is read as
    ready by everything that does not parse it.
    """
    state = request.app.state
    if state.upstream is None:
        return JSONResponse(
            {"status": "not_ready", "reason": "upstream client is not initialised"},
            status_code=503,
        )

    result = await state.upstream.reachable()
    if result.ready:
        return JSONResponse(
            {
                "status": "ready",
                "instance_id": state.instance_id,
                "upstream": result.reason,
            }
        )
    return JSONResponse(
        {
            "status": "not_ready",
            "instance_id": state.instance_id,
            "reason": result.reason,
        },
        status_code=503,
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def create_app(
    config: AutoRouterConfig | None = None,
    *,
    instance_id: str | None = None,
    max_body_bytes: int | None = None,
    upstream_client: UpstreamClient | None = None,
) -> Starlette:
    """Build the inference app. Pure -- no I/O, no sockets, no schema loading.

    Args:
        config: Loaded configuration. ``load_config(None)`` when omitted, which
            falls through to built-in defaults when no config file exists.
        instance_id: This process's supervision identity. Minted when omitted.
            ``main.py`` mints it once and passes the same value here and into the
            runtime file; see the module docstring for why they must agree by
            construction rather than by shared lookup.
        max_body_bytes: Request-body ceiling. See
            ``ingress.MAX_REQUEST_BYTES`` for why this is a parameter and not a
            config key today.
        upstream_client: Pre-built client, for tests. When supplied it is used
            as-is and **not** closed on shutdown -- whoever built it owns it.
    """
    if config is None:
        config = load_config(None)

    resolved_instance_id = instance_id or new_instance_id()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Everything expensive, once, at the point a server really starts.

        Starlette 1.x removed ``on_startup``/``on_shutdown``; this is the only
        remaining hook. It is an explicit ``asynccontextmanager`` because
        ``Router.__init__`` emits a deprecation warning for a bare async
        generator.
        """
        directory = resolve_state_dir(config.gateway.state_dir)
        app.state.logger = get_logger("hermes_auto.gateway", directory=directory)

        # Hoist every validator here. Building one costs a `check_schema` pass of
        # roughly 50 ms; paying that on a user's first prompt is a TTFT
        # regression that no later measurement would attribute correctly.
        ingress.build_validators()

        token = read_token(config.gateway.state_dir)
        if token is None:
            token = mint_token(config.gateway.state_dir)
            app.state.logger.info({"event": "auth.token.minted"})
        app.state.token = token

        owns_client = upstream_client is None
        app.state.upstream = upstream_client or UpstreamClient(config)
        app.state.logger.info(
            {
                "event": "gateway.started",
                "instance_id": resolved_instance_id,
                "upstream_base_url": config.upstream.base_url,
                "strict_validation": config.gateway.strict_validation,
            }
        )
        try:
            yield
        finally:
            if owns_client and app.state.upstream is not None:
                await app.state.upstream.aclose()
            app.state.logger.info(
                {"event": "gateway.stopped", "instance_id": resolved_instance_id}
            )

    app = Starlette(
        routes=[
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/v1/models", list_models, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", readyz, methods=["GET"]),
        ],
        exception_handlers={
            GatewayError: gateway_error_handler,
            HTTPException: http_exception_handler,
            Exception: unhandled_exception_handler,
        },
        lifespan=lifespan,
    )

    # Set before startup so create_app()'s result is inspectable, and so a
    # handler reached without a lifespan (a bare ASGI call in a test) fails on a
    # 401 rather than an AttributeError.
    app.state.config = config
    app.state.instance_id = resolved_instance_id
    app.state.token = None
    app.state.upstream = None
    app.state.strict_validation = config.gateway.strict_validation
    app.state.max_body_bytes = (
        ingress.MAX_REQUEST_BYTES if max_body_bytes is None else max_body_bytes
    )
    app.state.logger = get_logger("hermes_auto.gateway")
    return app


# Re-exported so a caller catching gateway failures does not have to know which
# module raised. Not used above; kept for callers of this module.
_ = error_response
