"""R5's central claim, tested the only way it can be: differentially.

Every assertion here compares **the gateway against the mock it forwards to**.
A test that only checked "the gateway returned something SSE-shaped" would pass
against a relay that reassembled every frame, which is precisely the bug the
fixture corpus exists to catch. So each of plan 02-03's twelve fixtures is
fetched twice -- once straight from the mock, once through the gateway -- and the
two byte sequences are required to be equal.

**The gateway runs under real uvicorn on a real loopback socket, in its own
thread with its own event loop.** Neither of the in-process shortcuts works for
this suite:

* ``starlette.testclient.TestClient`` writes the whole response into a
  ``BytesIO`` before returning it (``testclient.py``, ``raw_kwargs["stream"]``),
  so it cannot express a client that hangs up mid-stream.
* ``httpx.ASGITransport`` joins every body part into one chunk
  (``ASGIResponseStream.__aiter__`` does ``b"".join(self._body)``), so it cannot
  observe streaming behaviour either, and it never delivers ``http.disconnect``
  while a response is in flight.

Running the real server also exercises the ASGI-spec-version branch that matters:
uvicorn advertises ``spec_version 2.3``, which sends Starlette's
``StreamingResponse`` down its task-group-plus-``listen_for_disconnect`` path.
That is the path a client disconnect actually travels.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import struct
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn

from hermes_auto.config import AutoRouterConfig, GatewayConfig, UpstreamConfig
from hermes_auto.gateway.app import VIRTUAL_MODELS, create_app
from hermes_auto.gateway.auth import mint_token
from hermes_auto.gateway.errors import assert_error_body
from hermes_auto.gateway.upstream import (
    HOP_BY_HOP_HEADERS,
    UpstreamClient,
    filter_request_headers,
    filter_response_headers,
)
from tests.integration.mock_upstream import (
    FIXTURES,
    MockUpstream,
    fixture_bytes,
    iter_fixture_names,
)

pytestmark = pytest.mark.contract

FIXTURE_NAMES = sorted(FIXTURES)

#: Fixtures whose upstream response is deliberately truncated: the mock closes
#: the connection without the terminating chunk, so a correct client raises on
#: both the direct and the relayed read.
TRUNCATED = {"midstream_disconnect"}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class RunningGateway:
    """The gateway app served by uvicorn on an ephemeral loopback port.

    Started in a daemon thread with its own loop so the test's own event loop
    (pytest-asyncio, function-scoped) and the server's are fully independent, and
    so the client talks to it over a real TCP connection rather than through an
    in-process transport that would smooth over the framing under test.
    """

    def __init__(self, app: Any) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(64)
        self.port = int(self._socket.getsockname()[1])
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="warning",
                lifespan="on",
                access_log=False,
                server_header=False,
            )
        )
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [self._socket]},
            daemon=True,
            name=f"gateway-{self.port}",
        )

    def __enter__(self) -> RunningGateway:
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("gateway did not start within 20s")
            if not self._thread.is_alive():
                raise RuntimeError("gateway thread died during startup")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=20)


@pytest.fixture()
def state_dir(tmp_path, monkeypatch) -> Any:
    """An isolated state directory, so no test can touch a real install.

    ``HERMES_AUTO_STATE_DIR`` outranks configuration by design
    (``state/paths.py``), which is what makes this isolation structural rather
    than a convention every test has to remember.
    """
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(directory))
    return directory


@pytest.fixture()
def token(state_dir) -> str:
    """A real minted token, through the real minting path."""
    return mint_token()


@pytest.fixture()
def upstream() -> Any:
    with MockUpstream() as mock:
        yield mock


def make_config(
    upstream_url: str,
    *,
    strict_validation: bool = False,
) -> AutoRouterConfig:
    return AutoRouterConfig(
        gateway=GatewayConfig(strict_validation=strict_validation),
        upstream=UpstreamConfig(base_url=f"{upstream_url}/v1", model="fixed-target"),
    )


def envelope(**overrides: Any) -> dict[str, Any]:
    body = {
        "protocol_version": 1,
        "root_session_id": "session-abcdef",
        "virtual_model": "auto:balanced",
        "plugin_version": "0.1.0",
    }
    body.update(overrides)
    return body


def chat_body(*, stream: bool = True, with_envelope: bool = True, **extra: Any) -> dict:
    body: dict[str, Any] = {
        "model": "auto:balanced",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }
    if with_envelope:
        body["_hermes_auto"] = envelope()
    body.update(extra)
    return body


async def collect_raw(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> tuple[int, httpx.Headers, bytes, str | None]:
    """Read a response as raw bytes, reporting a transport failure rather than raising.

    ``aiter_raw`` and not ``aiter_bytes``: the point of the comparison is the
    undecoded transport body. The fourth element is the exception type name when
    the read ended in a transport error, which is how the truncation fixture is
    compared -- both sides must fail the same way, having delivered the same
    bytes first.
    """
    chunks: list[bytes] = []
    failure: str | None = None
    async with client.stream(method, url, **kwargs) as response:
        status = response.status_code
        headers = response.headers
        try:
            async for chunk in response.aiter_raw():
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            failure = type(exc).__name__
    return status, headers, b"".join(chunks), failure


_ID_FIELD = re.compile(rb'"id"\s*:\s*"[^"]*"')
_CREATED_FIELD = re.compile(rb'"created"\s*:\s*\d+')


def normalise(payload: bytes) -> bytes:
    """Blank out exactly the two fields the plan permits to differ."""
    payload = _ID_FIELD.sub(b'"id":"<id>"', payload)
    return _CREATED_FIELD.sub(b'"created":0', payload)


# ---------------------------------------------------------------------------
# 1. Byte identity across the whole corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
async def test_fixture_relays_byte_identically(
    fixture_name, upstream, state_dir, token
):
    """Every fixture, direct from the mock versus through the gateway.

    Asserted on the *raw* bytes as well as the normalised ones. Raw equality is
    the stronger claim and it holds here because the mock replays a fixed file:
    there is no clock or random id in the corpus for normalisation to hide. The
    normalised comparison is kept because it is the contract the plan states, and
    a future fixture that did carry a generated id would still be covered.
    """
    upstream.script(fixture_name)
    app = create_app(make_config(upstream.url), instance_id="test-instance")

    async with httpx.AsyncClient(timeout=30) as client:
        direct_status, direct_headers, direct_bytes, direct_failure = await collect_raw(
            client,
            "POST",
            f"{upstream.url}/v1/chat/completions",
            json=chat_body(),
        )
        with RunningGateway(app) as gateway:
            relayed_status, relayed_headers, relayed_bytes, relayed_failure = (
                await collect_raw(
                    client,
                    "POST",
                    f"{gateway.url}/v1/chat/completions",
                    json=chat_body(),
                    headers={"Authorization": f"Bearer {token}"},
                )
            )

    assert relayed_bytes == direct_bytes, (
        f"{fixture_name}: relayed body differs from direct body "
        f"({len(relayed_bytes)} vs {len(direct_bytes)} bytes)"
    )
    assert normalise(relayed_bytes) == normalise(direct_bytes)
    assert relayed_status == direct_status
    assert relayed_bytes == fixture_bytes(fixture_name)

    if fixture_name in TRUNCATED:
        assert direct_failure is not None, "fixture should truncate"
        assert relayed_failure == direct_failure, (
            "a truncated upstream must surface to the client as the same "
            "transport failure, not as a clean short body"
        )
    else:
        assert relayed_failure is None and direct_failure is None

    # Content-Type must survive: it is how the OpenAI SDK decides whether to
    # parse the body as a stream or as JSON.
    assert relayed_headers.get("content-type") == direct_headers.get("content-type")


async def test_crlf_and_lf_terminators_both_survive(upstream, state_dir, token):
    """LF and CRLF frame terminators are relayed as sent, not normalised.

    ``empty_deltas_and_keepalives`` is the one fixture using CRLF; the other
    eleven use LF. A relay that split on either terminator would rewrite one of
    them, and the byte comparison above would catch it -- this test names the
    property so a failure reads as what it is.
    """
    crlf = fixture_bytes("empty_deltas_and_keepalives")
    lf = fixture_bytes("text_stream")
    assert b"\r\n\r\n" in crlf and b"\r\n\r\n" not in lf

    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            for name, expected in (
                ("empty_deltas_and_keepalives", crlf),
                ("text_stream", lf),
            ):
                upstream.script(name)
                _, _, body, _ = await collect_raw(
                    client,
                    "POST",
                    f"{gateway.url}/v1/chat/completions",
                    json=chat_body(),
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert body == expected, name


@pytest.mark.parametrize("sizes", [[1], [7], [1097], [1097, 1, 1], [3, 5, 11, 13]])
async def test_relay_survives_arbitrary_upstream_rechunking(
    sizes, upstream, state_dir, token
):
    """Re-splitting the upstream bytes must not change what the client receives.

    ``fragmented_utf8`` carries raw multi-byte sequences and plan 02-03 computed
    offsets 1097-1099 to land mid-codepoint. A line-oriented or text-decoding
    relay fails here and nowhere else; this is the cheap sibling of plan 02-08's
    hypothesis fuzzer.
    """
    expected = fixture_bytes("fragmented_utf8")
    upstream.script("fragmented_utf8")
    upstream.set_rechunk(sizes)

    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=60) as client:
        with RunningGateway(app) as gateway:
            _, _, body, failure = await collect_raw(
                client,
                "POST",
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(),
                headers={"Authorization": f"Bearer {token}"},
            )
    assert failure is None
    assert body == expected


# ---------------------------------------------------------------------------
# 2. The envelope strip, and the opaque-dict passthrough
# ---------------------------------------------------------------------------


async def test_hermes_auto_is_stripped_before_forwarding(upstream, state_dir, token):
    """design.md 5.1: no upstream ever observes ``_hermes_auto``.

    Asserted against what the mock actually received, in both representations:
    the parsed body must not contain the key, and the raw bytes must not contain
    the string. The byte check is what catches a strip that removed the key from
    a parsed copy while forwarding the original bytes.
    """
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            await collect_raw(
                client,
                "POST",
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(),
                headers={"Authorization": f"Bearer {token}"},
            )

    assert len(upstream.requests) == 1
    received = upstream.requests[0]
    assert "_hermes_auto" not in received["json"]
    assert b"_hermes_auto" not in received["body"]
    assert b"session-abcdef" not in received["body"], (
        "the root session id must not reach the upstream in any form"
    )


async def test_unmodelled_fields_survive_the_round_trip(upstream, state_dir, token):
    """Fields this project has never heard of reach the upstream unchanged.

    This is the property that makes R5 hold for whatever OpenAI ships next, and
    it holds because nothing in the request path models the body. The nested and
    unicode cases are included because a re-serialiser that reordered keys or
    escaped non-ASCII would still pass a shallow check.
    """
    upstream.script("text_stream")
    exotic = {
        "future_field": {"x": 1, "nested": [1, 2, {"deep": True}]},
        "prediction": {"type": "content", "content": "guess"},
        "reasoning_effort": "high",
        "unicode_field": "é中\U0001f600",
        "null_field": None,
        "zero": 0,
        "false": False,
        "empty_list": [],
        "empty_object": {},
    }
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            await collect_raw(
                client,
                "POST",
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(**exotic),
                headers={"Authorization": f"Bearer {token}"},
            )

    received = upstream.requests[0]["json"]
    for key, value in exotic.items():
        assert key in received, f"{key} was dropped"
        assert received[key] == value, f"{key} was altered"
    assert received["messages"] == [{"role": "user", "content": "hello"}]
    assert received["model"] == "auto:balanced"


async def test_absent_envelope_is_not_an_error(upstream, state_dir, token):
    """A plain OpenAI request with no ``_hermes_auto`` is served normally.

    Absent is not corrupt. The gateway is an OpenAI-compatible endpoint, and a
    caller reaching it without the provider plugin -- a curl, a health script,
    another tool -- must not get a 400 for a field only the plugin sets.
    """
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            status, _, body, _ = await collect_raw(
                client,
                "POST",
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(with_envelope=False),
                headers={"Authorization": f"Bearer {token}"},
            )
    assert status == 200
    assert body == fixture_bytes("text_stream")


@pytest.mark.parametrize(
    ("bad_envelope", "expected_code"),
    [
        (envelope(protocol_version=2), "protocol_version_mismatch"),
        (envelope(protocol_version="1"), "protocol_version_mismatch"),
        # 02-CONTEXT risk: Hermes calls build_extra_body with no session_id for
        # compression, vision and title generation. A plugin emitting
        # `session_id or ""` produces exactly this envelope, and the schema's
        # minLength: 1 rejects it. The gateway's job is to reject it clearly;
        # supplying a synthesised id is the provider plugin's job (plan 02-05).
        (envelope(root_session_id=""), "invalid_routing_metadata"),
        (envelope(virtual_model=""), "invalid_routing_metadata"),
        ({"protocol_version": 1}, "invalid_routing_metadata"),
        (envelope(unexpected_key="x"), "invalid_routing_metadata"),
    ],
)
async def test_invalid_envelope_is_rejected_in_the_error_shape(
    bad_envelope, expected_code, upstream, state_dir, token
):
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    body = chat_body()
    body["_hermes_auto"] = bad_envelope

    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 400, response.text
    payload = response.json()
    assert_error_body(payload)
    assert payload["error"]["code"] == expected_code
    assert upstream.requests == [], "a rejected envelope must not reach the upstream"
    # The raw session id must never be echoed back to the caller.
    assert "session-abcdef" not in response.text


async def test_empty_session_id_never_reaches_the_upstream(upstream, state_dir, token):
    """The absent-session_id case, stated as its own regression.

    Named separately from the parametrised case above because this is the one
    the phase critique flagged as execution-breaking: it would silently 400 every
    compression, vision and title-generation call.
    """
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    body = chat_body()
    body["_hermes_auto"] = envelope(root_session_id="")
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 400
    assert "root_session_id" in response.json()["error"]["message"]
    assert upstream.requests == []


# ---------------------------------------------------------------------------
# 3. Authentication
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({"Authorization": "Bearer wrong-token"}, id="wrong"),
        pytest.param({"Authorization": "Bearer"}, id="empty"),
        pytest.param({"Authorization": "token-without-scheme"}, id="no-scheme"),
        pytest.param({"Authorization": "Basic dXNlcjpwdw=="}, id="wrong-scheme"),
    ],
)
async def test_bad_auth_is_401_in_the_error_envelope(
    headers, upstream, state_dir, token
):
    """Every rejection returns the same status, code, and message.

    Identical bodies matter: a message that differed between "no token" and
    "wrong token" would leak the same fact the constant-time comparison exists
    to hide, in a channel far easier to read than timing.
    """
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(),
                headers=headers,
            )
    assert response.status_code == 401
    payload = response.json()
    assert_error_body(payload)
    assert payload["error"]["code"] == "invalid_api_key"
    assert upstream.requests == [], "auth must fail before any upstream connection"


async def test_missing_and_wrong_token_produce_identical_bodies(
    upstream, state_dir, token
):
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            absent = await client.post(
                f"{gateway.url}/v1/chat/completions", json=chat_body()
            )
            wrong = await client.post(
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(),
                headers={"Authorization": f"Bearer {'x' * len(token)}"},
            )
    assert absent.status_code == wrong.status_code == 401
    assert absent.content == wrong.content


async def test_models_requires_authentication(upstream, state_dir, token):
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            unauthorised = await client.get(f"{gateway.url}/v1/models")
            authorised = await client.get(
                f"{gateway.url}/v1/models",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert unauthorised.status_code == 401
    assert_error_body(unauthorised.json())
    assert authorised.status_code == 200


# ---------------------------------------------------------------------------
# 4. Limits
# ---------------------------------------------------------------------------


async def test_oversized_body_is_413_and_opens_no_upstream_connection(
    upstream, state_dir, token
):
    """413 before dialling. ``.requests`` being empty is the whole assertion."""
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url), max_body_bytes=2048)
    oversized = chat_body()
    oversized["messages"] = [{"role": "user", "content": "x" * 8192}]

    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                json=oversized,
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 413
    payload = response.json()
    assert_error_body(payload)
    assert payload["error"]["code"] == "request_too_large"
    assert upstream.requests == []


async def test_body_just_under_the_cap_is_accepted(upstream, state_dir, token):
    """The cap must not be off by enough to reject legitimate traffic."""
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url), max_body_bytes=8192)
    body = chat_body()
    body["messages"] = [{"role": "user", "content": "y" * 1000}]
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            status, _, payload, _ = await collect_raw(
                client,
                "POST",
                f"{gateway.url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    assert status == 200
    assert payload == fixture_bytes("text_stream")
    assert len(upstream.requests) == 1


async def test_non_json_content_type_is_415(upstream, state_dir, token):
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                content=b"model=auto",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
    assert response.status_code == 415
    assert_error_body(response.json())
    assert upstream.requests == []


async def test_malformed_json_is_400_not_500(upstream, state_dir, token):
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                content=b'{"model": "auto:balanced", ',
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
    assert response.status_code == 400
    assert_error_body(response.json())
    assert upstream.requests == []


# ---------------------------------------------------------------------------
# 5. Upstream failures are relayed, not reinterpreted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "expected_status"),
    [("error_401", 401), ("error_429", 429), ("error_context_length", 400)],
)
async def test_upstream_errors_are_relayed_with_status_and_body_intact(
    fixture_name, expected_status, upstream, state_dir, token
):
    """The gateway does not reinterpret a provider error.

    A gateway that turned an upstream 429 into a 502 -- or into its own 400 --
    would break every client retry policy keyed on the provider's status, and
    would violate R5 on the error path just as surely as on the success path.
    """
    upstream.script(fixture_name)
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(),
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == expected_status
    assert response.content == fixture_bytes(fixture_name)
    assert_error_body(response.json())
    if fixture_name == "error_429":
        assert response.headers.get("retry-after") == "20", (
            "dropping Retry-After turns a recoverable throttle into an opaque "
            "failure for the caller"
        )


async def test_unreachable_upstream_is_502_and_not_a_truncated_200(state_dir, token):
    """Connection refused must not reach the client as an empty success."""
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()

    app = create_app(make_config(f"http://127.0.0.1:{dead_port}"))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(),
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 502
    payload = response.json()
    assert_error_body(payload)
    assert payload["error"]["code"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# 6. Hop-by-hop headers
# ---------------------------------------------------------------------------


def test_hop_by_hop_filter_covers_the_declared_set():
    """The drop set is the RFC 9110 connection-specific list, both directions."""
    assert HOP_BY_HOP_HEADERS == {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
    }
    sample = [(name.title(), "value") for name in sorted(HOP_BY_HOP_HEADERS)]
    sample.append(("X-Keep-Me", "yes"))
    assert filter_request_headers(sample) == [("X-Keep-Me", "yes")]
    assert filter_response_headers(sample) == [("X-Keep-Me", "yes")]


def test_request_filter_also_drops_the_gateways_own_credential():
    """The inbound bearer token is *this gateway's*; it must not go upstream."""
    forwarded = filter_request_headers(
        [
            ("Authorization", "Bearer local-gateway-token"),
            ("Host", "127.0.0.1:8787"),
            ("Content-Length", "123"),
            ("Accept-Encoding", "gzip"),
            ("X-Stainless-Lang", "python"),
        ]
    )
    assert forwarded == [("X-Stainless-Lang", "python")]


async def test_hop_by_hop_headers_are_not_forwarded_upstream(
    upstream, state_dir, token
):
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            await collect_raw(
                client,
                "POST",
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Keep-Alive": "timeout=5, max=1000",
                    "Proxy-Authorization": "Basic c2VjcmV0",
                    "TE": "trailers",
                    "X-Stainless-Runtime": "CPython",
                },
            )

    received = {k.lower(): v for k, v in upstream.requests[0]["headers"].items()}
    assert "keep-alive" not in received
    assert "proxy-authorization" not in received
    assert "te" not in received
    assert received.get("x-stainless-runtime") == "CPython", (
        "non-hop-by-hop client headers must be forwarded"
    )
    assert received.get("authorization") != f"Bearer {token}", (
        "the gateway's own bearer token must never be forwarded upstream"
    )


async def test_upstream_connection_header_is_not_relayed_to_the_client(
    upstream, state_dir, token
):
    """The SSE fixtures send ``Connection: keep-alive``; it must not be copied.

    A relayed ``Connection`` header describes the gateway-to-upstream hop, not
    the client-to-gateway one, and copying it is how a relay ends up telling a
    client to keep a connection the server is about to close.
    """
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            async with client.stream(
                "POST",
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(),
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                relayed = list(response.headers.multi_items())
                await response.aread()

    assert ("x-accel-buffering", "no") in [(k.lower(), v) for k, v in relayed], (
        "non-hop-by-hop upstream headers must be relayed"
    )
    # h11 supplies its own connection management; the upstream's value must not
    # appear twice or as a copied duplicate.
    connection_values = [v for k, v in relayed if k.lower() == "connection"]
    assert len(connection_values) <= 1


# ---------------------------------------------------------------------------
# 7. Client disconnect cancels the upstream
# ---------------------------------------------------------------------------


class _SlowUpstream(UpstreamClient):
    """A stand-in that streams slowly and records when it is released.

    A real upstream on loopback answers faster than any test can abort, so a
    disconnect test driven by the mock would race: the stream would usually have
    finished before the client hung up, and the assertion would pass without ever
    exercising cancellation. Substituting a deliberately slow source removes the
    race, and everything downstream of it -- the relay, ``_RelayResponse``,
    Starlette's disconnect handling, uvicorn -- is the real production path.
    """

    def __init__(self, chunks: int = 400, interval: float = 0.02) -> None:
        super().__init__(UpstreamConfig())
        self.total = chunks
        self.interval = interval
        self.emitted = 0
        self.released = threading.Event()

    async def stream(self, body, headers=None, *, path="/chat/completions"):
        parent = self

        async def source():
            try:
                for index in range(parent.total):
                    parent.emitted = index + 1
                    yield b'data: {"n":%d}\n\n' % index
                    await asyncio.sleep(parent.interval)
            finally:
                parent.released.set()

        return 200, httpx.Headers({"content-type": "text/event-stream"}), source()


def _abort_mid_stream(port: int, payload: bytes, token: str, want_bytes: int) -> int:
    """Send a request, read part of the body, then reset the connection.

    ``SO_LINGER`` with a zero timeout makes ``close()`` emit a TCP RST instead of
    a FIN, so the server observes the disconnect immediately rather than at the
    end of a graceful shutdown -- which is what a killed client actually does.
    """
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer " + token.encode() + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
        b"\r\n" + payload
    )
    connection = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        connection.sendall(request)
        received = b""
        while len(received) < want_bytes:
            chunk = connection.recv(4096)
            if not chunk:
                break
            received += chunk
        connection.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        return len(received)
    finally:
        connection.close()


async def test_client_disconnect_releases_the_upstream_stream(state_dir, token):
    """A client that hangs up mid-stream must not leave a generation running.

    Two assertions, and the second is the one that matters. That the iterator's
    ``finally`` ran proves the release happened; that far fewer chunks were
    emitted than the source would have produced proves it happened *because of
    the disconnect* rather than because the stream simply finished.
    """
    slow = _SlowUpstream(chunks=400, interval=0.02)
    app = create_app(make_config("http://127.0.0.1:1"), upstream_client=slow)
    payload = json.dumps(chat_body()).encode()

    with RunningGateway(app) as gateway:
        received = await asyncio.to_thread(
            _abort_mid_stream, gateway.port, payload, token, 200
        )
        assert received > 0, "no body reached the client before the abort"
        released = await asyncio.to_thread(slow.released.wait, 10.0)

    assert released, (
        "the upstream iterator was never released after the client disconnected; "
        "the generation would keep running (and billing)"
    )
    assert slow.emitted < slow.total, (
        f"the source ran to completion ({slow.emitted}/{slow.total} chunks), so "
        f"this test did not actually exercise cancellation"
    )


async def test_gateway_still_serves_after_a_client_aborts(upstream, state_dir, token):
    """An aborted request must not wedge the gateway or leak a connection."""
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    payload = json.dumps(chat_body()).encode()

    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            await asyncio.to_thread(_abort_mid_stream, gateway.port, payload, token, 1)
            upstream.clear_requests()
            status, _, body, failure = await collect_raw(
                client,
                "POST",
                f"{gateway.url}/v1/chat/completions",
                json=chat_body(),
                headers={"Authorization": f"Bearer {token}"},
            )
    assert failure is None
    assert status == 200
    assert body == fixture_bytes("text_stream")


# ---------------------------------------------------------------------------
# 8. The other three routes
# ---------------------------------------------------------------------------


async def test_models_lists_the_four_virtual_lanes(upstream, state_dir, token):
    """``/v1/models`` reports the router's lanes and does not proxy upstream."""
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            first = await client.get(
                f"{gateway.url}/v1/models",
                headers={"Authorization": f"Bearer {token}"},
            )
            second = await client.get(
                f"{gateway.url}/v1/models",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert first.status_code == 200
    payload = first.json()
    assert payload["object"] == "list"
    assert [entry["id"] for entry in payload["data"]] == list(VIRTUAL_MODELS)
    assert set(VIRTUAL_MODELS) == {
        "auto:quality",
        "auto:balanced",
        "auto:economy",
        "auto:session",
    }
    assert upstream.requests == [], "/v1/models must not proxy the upstream"
    assert first.content == second.content, "the model list must be byte-stable"


async def test_healthz_echoes_this_processes_instance_id(upstream, state_dir):
    """The PID-reuse defence: ``/healthz`` reports *this process's* identity.

    If it echoed the runtime file instead, every process sharing a state
    directory would return the same value and plan 02-07's mismatch branch --
    "another process owns this port" -- would be unreachable. Two apps with
    different ids on different ports must report differently.
    """
    app_a = create_app(make_config(upstream.url), instance_id="instance-aaaa")
    app_b = create_app(make_config(upstream.url), instance_id="instance-bbbb")
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app_a) as a, RunningGateway(app_b) as b:
            first = await client.get(f"{a.url}/healthz")
            second = await client.get(f"{b.url}/healthz")

    assert first.status_code == 200
    assert first.json() == {"status": "ok", "instance_id": "instance-aaaa"}
    assert second.json() == {"status": "ok", "instance_id": "instance-bbbb"}


async def test_healthz_is_true_while_the_upstream_is_down(state_dir):
    """Liveness must not depend on the upstream. Readiness must."""
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()

    app = create_app(
        make_config(f"http://127.0.0.1:{dead_port}"), instance_id="alive-anyway"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            health = await client.get(f"{gateway.url}/healthz")
            ready = await client.get(f"{gateway.url}/readyz")

    assert health.status_code == 200
    assert health.json()["instance_id"] == "alive-anyway"
    assert ready.status_code == 503, "readiness must fail when the upstream is down"
    assert ready.json()["status"] == "not_ready"
    assert "reason" in ready.json()


async def test_readyz_is_200_when_the_upstream_accepts_connections(
    upstream, state_dir
):
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            ready = await client.get(f"{gateway.url}/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


async def test_unknown_path_returns_the_error_envelope(upstream, state_dir, token):
    """A typo must not return text/plain that an OpenAI SDK cannot parse."""
    app = create_app(make_config(upstream.url))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            missing = await client.get(f"{gateway.url}/v1/completions")
            wrong_method = await client.get(f"{gateway.url}/v1/chat/completions")

    assert missing.status_code == 404
    assert_error_body(missing.json())
    assert wrong_method.status_code == 405
    assert_error_body(wrong_method.json())


# ---------------------------------------------------------------------------
# 9. Structural guarantees the byte tests rest on
# ---------------------------------------------------------------------------


def test_no_pydantic_anywhere_in_the_gateway():
    """FastAPI/Pydantic would model the body and silently drop unmodelled fields.

    ``02-CONTEXT.md`` refuses both by name. Checked at the module level rather
    than by grepping source, so an import added indirectly is still caught.
    """
    import sys

    import hermes_auto.gateway.app  # noqa: F401
    import hermes_auto.gateway.ingress  # noqa: F401
    import hermes_auto.gateway.upstream  # noqa: F401

    assert "pydantic" not in sys.modules
    assert "fastapi" not in sys.modules


def test_relay_module_cannot_reassemble_frames():
    """No line iterator, no text conversion, no buffer, in the relay module."""
    import ast
    import inspect

    import hermes_auto.gateway.relay as relay

    source = inspect.getsource(relay)
    assert "aiter_lines" not in source
    assert "decode(" not in source
    tree = ast.parse(source)
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    for forbidden in ("aiter_lines", "aiter_text", "aiter_bytes", "decode", "split"):
        assert forbidden not in attributes, f"relay references {forbidden}"


def test_no_backend_adapter_or_routing_seam_was_introduced():
    """02-CONTEXT rejected a speculative adapter Protocol in this phase.

    Checked against defined *names*, not against source text. A substring grep
    fails on the docstrings that explain the rejection -- which is the wrong
    result twice over: it punishes documenting the decision, and it would still
    pass on an adapter named something else. What is actually forbidden is a
    ``Protocol``, an abstract base, or a second implementation of the upstream
    seam existing in these modules.
    """
    import ast
    import inspect
    import typing

    import hermes_auto.gateway.app as app_module
    import hermes_auto.gateway.upstream as upstream_module

    for module in (app_module, upstream_module):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {
                    base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    for base in node.bases
                }
                assert "Protocol" not in bases, f"{node.name} is a Protocol"
                assert "ABC" not in bases, f"{node.name} is an abstract base"
            if isinstance(node, ast.ImportFrom) and node.module in {"typing", "abc"}:
                imported = {alias.name for alias in node.names}
                assert not imported & {
                    "Protocol",
                    "runtime_checkable",
                    "ABC",
                    "abstractmethod",
                }, f"{module.__name__} imports an abstraction seam: {imported}"

    # There is exactly one upstream implementation, and it is a plain class.
    assert UpstreamClient.__bases__ == (object,), UpstreamClient.__bases__
    # No routing vocabulary in the seam's own declared surface. Checked against
    # __all__ rather than every module global, so an imported name this module
    # merely consumes -- AutoRouterConfig -- is not mistaken for one it exports.
    exported = set(upstream_module.__all__)
    for word in ("candidate", "score", "select", "route", "adapter", "backend"):
        assert not any(word in name.lower() for name in exported), (
            f"routing vocabulary {word!r} in the upstream seam: {sorted(exported)}"
        )


async def test_state_directory_isolation_is_in_effect(state_dir):
    """Guards every other test here: nothing may touch a real install."""
    assert os.environ["HERMES_AUTO_STATE_DIR"] == str(state_dir)
    assert "hermes-auto" not in str(state_dir) or str(state_dir).startswith(
        str(state_dir.parent)
    )
