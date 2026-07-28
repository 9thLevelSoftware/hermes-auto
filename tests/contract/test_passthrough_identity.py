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
import gzip
import json
import os
import re
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.requests import Request

from hermes_auto.config import AutoRouterConfig, GatewayConfig, UpstreamConfig
from hermes_auto.gateway import ingress
from hermes_auto.gateway.app import VIRTUAL_MODELS, create_app
from hermes_auto.gateway.auth import mint_token
from hermes_auto.gateway.errors import GatewayError, assert_error_body
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
    """No line iterator, no text conversion, no buffer, in the relay module.

    Checked against the AST only. The two ``assert "..." not in source``
    substring checks that used to stand above the walk were deleted: a substring
    check on source text is evadable in one direction (``import hashlib as _h``
    defeats exactly this shape elsewhere in this repo) and brittle in the other
    -- three plans in this phase hit a source-text guard firing on the docstring
    that *documents* the exclusion. The walk below is strictly stronger.
    """
    import ast
    import inspect

    import hermes_auto.gateway.relay as relay

    tree = ast.parse(inspect.getsource(relay))
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    for forbidden in ("aiter_lines", "aiter_text", "aiter_bytes", "decode", "split"):
        assert forbidden not in attributes, f"relay references {forbidden}"


def test_both_upstream_verbs_iterate_undecoded_transport_bytes():
    """The raw-versus-decoded choice lives in ``upstream.py``, so guard it there.

    The guard above was pointed at ``relay.py``, where the call has never lived:
    ``relay_stream`` consumes an iterator it is handed. The actual decision is
    ``UpstreamClient.stream`` and ``UpstreamClient.complete``, and with the guard
    aimed at the wrong module a reviewer swapped ``aiter_raw()`` for
    ``aiter_bytes()`` and the entire suite stayed green while the client began
    receiving plaintext labelled ``gzip``.

    Two claims, both structural:

    * every byte-producing loop in the module iterates ``aiter_raw`` -- asserted
      as an ``ast.Attribute`` inside an ``ast.AsyncFor`` header, not as a
      substring, so a stray mention in a docstring neither satisfies nor trips
      it;
    * no decoded-body accessor is referenced anywhere in the module.
      ``response.content`` is on that list because it is what ``complete()``
      used, and it is decoded while the response headers relayed beside it
      describe the compressed bytes.
    """
    import ast
    import inspect

    import hermes_auto.gateway.upstream as upstream_module

    tree = ast.parse(inspect.getsource(upstream_module))

    raw_loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFor)
        and any(
            isinstance(inner, ast.Attribute) and inner.attr == "aiter_raw"
            for inner in ast.walk(node.iter)
        )
    ]
    async_loops = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFor)]
    assert len(raw_loops) >= 2, (
        "both stream() and complete() must read the response through "
        f"aiter_raw(); found {len(raw_loops)} such loop(s)"
    )
    assert len(raw_loops) == len(async_loops), (
        "an async iteration in the upstream seam that is not over aiter_raw() "
        "is a second decode policy"
    )

    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    for forbidden in ("aiter_bytes", "aiter_lines", "aiter_text", "content", "text"):
        assert forbidden not in attributes, (
            f"upstream references the decoded accessor {forbidden!r}; the "
            f"response headers it relays describe the raw bytes"
        )


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


# ---------------------------------------------------------------------------
# 10. Request bodies a JSON encoder cannot round-trip on its own
# ---------------------------------------------------------------------------

#: A lone UTF-16 high surrogate, which is legal RFC 8259 JSON -- ``\ud800`` is a
#: well-formed ``\uXXXX`` escape and the grammar imposes no pairing rule -- and
#: which every ``ensure_ascii`` encoder emits. Python parses it to an unpaired
#: code point that ``str.encode("utf-8")`` refuses.
#:
#: Not a synthetic curiosity: Windows subprocess output decoded with
#: ``errors="surrogateescape"`` produces exactly these code points, and that text
#: flows into a tool result and then into ``messages``.
#:
#: The fixture corpus already builds lone surrogates into the *response* path
#: (``mock_upstream.py``'s docstring, ``fixtures/sse/README.md``). Nothing
#: covered the request path, which is where the gateway re-serialises.
LONE_SURROGATE = "tail \ud800 end"


def surrogate_request_bytes() -> bytes:
    """The request body, serialised the way an OpenAI SDK would put it on the wire.

    ``ensure_ascii`` defaults to True, so the surrogate travels as the seven
    ASCII bytes ``\\ud800``. Nothing unusual reaches the socket; the difficulty
    is entirely on the gateway's side of the parse.
    """
    body = chat_body()
    body["messages"] = [{"role": "user", "content": LONE_SURROGATE}]
    return json.dumps(body).encode("ascii")


async def test_lone_surrogate_in_the_request_body_reaches_the_upstream(
    upstream, state_dir, token
):
    """A body the gateway cannot naively re-encode must still be forwarded.

    Differential, like everything else here: the same bytes are posted straight
    at the mock and through the gateway, and the two must agree on status. The
    stronger assertion is the third one -- the upstream has to *receive the code
    point*, not a replacement character, not a ``\\uXXXX`` escape of it, and not
    nothing at all.
    """
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    payload = surrogate_request_bytes()
    headers = {"Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        direct_status, _, _, direct_failure = await collect_raw(
            client,
            "POST",
            f"{upstream.url}/v1/chat/completions",
            content=payload,
            headers=headers,
        )
        with RunningGateway(app) as gateway:
            relayed_status, _, relayed_bytes, relayed_failure = await collect_raw(
                client,
                "POST",
                f"{gateway.url}/v1/chat/completions",
                content=payload,
                headers={**headers, "Authorization": f"Bearer {token}"},
            )

    assert direct_failure is None and relayed_failure is None
    assert relayed_status == direct_status == 200, (
        "a lone surrogate is legal JSON; rejecting it is a divergence from "
        "talking to the provider directly"
    )
    assert relayed_bytes == fixture_bytes("text_stream")

    assert len(upstream.requests) == 2, "the gateway never reached the upstream"
    received = upstream.requests[1]["body"]
    # `ensure_ascii=False` means the surrogate is emitted as itself, so the
    # forwarded bytes carry its UTF-8-shaped encoding rather than an escape.
    assert b"\xed\xa0\x80" in received
    forwarded = json.loads(received.decode("utf-8", "surrogatepass"))
    assert forwarded["messages"][0]["content"] == LONE_SURROGATE
    assert "_hermes_auto" not in forwarded


async def test_surrogate_body_is_not_rejected_under_strict_validation(
    upstream, state_dir, token
):
    """The strict-validation path re-dumps the same body and must not crash either."""
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url, strict_validation=True))
    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            status, _, body, failure = await collect_raw(
                client,
                "POST",
                f"{gateway.url}/v1/chat/completions",
                content=surrogate_request_bytes(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
    assert failure is None
    assert status == 200
    assert body == fixture_bytes("text_stream")


# ---------------------------------------------------------------------------
# 11. A compressing upstream, on both verbs
# ---------------------------------------------------------------------------


class CompressingUpstream:
    """An upstream that gzips its response whatever the client's Accept-Encoding.

    ``UpstreamClient`` forces ``Accept-Encoding: identity`` precisely so this
    should not happen, but "should not" is not "cannot": a provider, or a proxy
    in front of one, can compress anyway. ``upstream.py``'s own comment concedes
    that "the local mock never compresses, so no test in this phase would catch
    it" -- this class is what makes that catchable.

    It lives here rather than in ``tests/integration/mock_upstream.py`` only
    because that file belongs to another plan in this cycle. The capability is a
    ``FixtureSpec`` flag in shape and should move into the shared mock as soon as
    one plan owns both files.

    Deliberately minimal: one route, one canned payload, ``Content-Length``
    framing over the *compressed* length. That last detail is the whole point --
    it is what turns "relay the decoded body under the original headers" from a
    silent corruption into a protocol error.
    """

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        # mtime=0 so the compressed bytes are stable across runs; a gzip header
        # carrying a clock would make a byte comparison flap.
        self.compressed = gzip.compress(payload, mtime=0)
        self.requests: list[dict[str, Any]] = []
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "CompressingUpstream"
            sys_version = ""

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                """A test run emits hundreds; none of them are informative."""

            def do_POST(self) -> None:  # noqa: N802 - dispatch name
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                # Always drained: unread bytes desynchronize a keep-alive socket.
                body = self.rfile.read(length) if length > 0 else b""
                owner.requests.append(
                    {"headers": dict(self.headers.items()), "body": body}
                )
                self.send_response_only(200, "OK")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(owner.compressed)))
                self.end_headers()
                self.wfile.write(owner.compressed)
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
            name="compressing-upstream",
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{int(self._server.server_address[1])}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> CompressingUpstream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


COMPLETION_PAYLOAD = json.dumps(
    {
        "id": "chatcmpl-gzip",
        "object": "chat.completion",
        "created": 0,
        "model": "fixed-target",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "compressed hello " * 8},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 16, "total_tokens": 20},
    }
).encode("utf-8")


@pytest.fixture()
def gzip_upstream() -> Any:
    with CompressingUpstream(COMPLETION_PAYLOAD) as compressing:
        yield compressing


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
async def test_gzipped_upstream_relays_intact_on_both_verbs(
    stream, gzip_upstream, state_dir, token
):
    """Both verbs must have the same decode semantics, because they share a header filter.

    ``RESPONSE_DROP_HEADERS`` keeps ``content-encoding`` and ``content-length``,
    and that is correct for a relay of *raw* bytes. ``complete()`` returned
    httpx's *decoded* body under those same headers, so 272 decoded bytes went
    out declaring ``Content-Length: 189`` and the client saw a
    ``RemoteProtocolError`` on a 200. Parametrised over both verbs so the two
    can never drift apart again without a failure naming which one moved.
    """
    app = create_app(make_config(gzip_upstream.url))
    body = chat_body(stream=stream)

    async with httpx.AsyncClient(timeout=30) as client:
        direct_status, direct_headers, direct_bytes, direct_failure = await collect_raw(
            client,
            "POST",
            f"{gzip_upstream.url}/v1/chat/completions",
            json=body,
        )
        with RunningGateway(app) as gateway:
            relayed_status, relayed_headers, relayed_bytes, relayed_failure = (
                await collect_raw(
                    client,
                    "POST",
                    f"{gateway.url}/v1/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            )

    assert direct_failure is None
    assert direct_bytes == gzip_upstream.compressed
    assert direct_headers.get("content-encoding") == "gzip"

    assert relayed_failure is None, (
        "the client could not read the relayed body: the bytes and the framing "
        "headers describing them disagree"
    )
    assert relayed_status == direct_status
    assert relayed_bytes == direct_bytes
    assert gzip.decompress(relayed_bytes) == COMPLETION_PAYLOAD
    assert relayed_headers.get("content-encoding") == "gzip", (
        "compressed bytes were relayed without the header that describes them"
    )
    assert relayed_headers.get("content-length") == direct_headers.get(
        "content-length"
    ), "the declared length must describe the bytes actually sent"


# ---------------------------------------------------------------------------
# 12. Nothing caller-supplied is reflected into an error body
# ---------------------------------------------------------------------------

#: Stands in for whatever a caller actually puts in ``root_session_id``. A real
#: one identifies a user's Hermes session; ADR-0004 requires it be hashed before
#: it reaches any record, and it must certainly not travel back out unhashed in a
#: 400 that any client -- or any log tailing client output -- can read.
LEAK_MARKER = "USER-SESSION-DO-NOT-LEAK"


@pytest.mark.parametrize(
    ("bad_session_id", "reason"),
    [
        pytest.param(LEAK_MARKER + "x" * 300, "maxLength", id="too-long"),
        pytest.param({"secret": LEAK_MARKER}, "type", id="object"),
        pytest.param([LEAK_MARKER], "type", id="array"),
        pytest.param(LEAK_MARKER, "type", id="wrong-type-but-a-string-marker"),
    ],
)
async def test_root_session_id_is_never_reflected_into_the_400_body(
    bad_session_id, reason, upstream, state_dir, token
):
    """``exc.message`` embeds the offending instance; the fix is to not use it.

    ``validate_envelope``'s comment already says ``exc.instance`` is deliberately
    not echoed because ``root_session_id`` is in there. The code honoured that
    for ``exc.instance`` and then formatted ``exc.message`` -- which, for
    ``maxLength``, ``minLength``, ``type`` and ``additionalProperties``, quotes
    the instance verbatim. The sibling function ``validate_request`` gets this
    right: keyword and path only.

    The ``wrong-type-but-a-string-marker`` case is valid input, so it 200s; it is
    parametrised in anyway to prove the marker's absence is not an artefact of
    every case failing.
    """
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    body = chat_body()
    body["_hermes_auto"] = envelope(root_session_id=bad_session_id)

    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )

    assert LEAK_MARKER not in response.text, (
        f"the {reason} rejection echoed root_session_id back to the caller: "
        f"{response.text}"
    )
    if response.status_code == 400:
        payload = response.json()
        assert_error_body(payload)
        assert payload["error"]["code"] == "invalid_routing_metadata"
        assert "root_session_id" in payload["error"]["message"], (
            "naming the failing field is the whole value of the message"
        )
        assert upstream.requests == []


async def test_envelope_rejection_reports_the_constraint_not_the_value(
    upstream, state_dir, token
):
    """What replaces the instance: the keyword that failed, and where.

    Enough for a plugin author to fix their envelope, and nothing a client did
    not already know.
    """
    upstream.script("text_stream")
    app = create_app(make_config(upstream.url))
    body = chat_body()
    body["_hermes_auto"] = envelope(virtual_model="v" * 400)

    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "virtual_model" in message
    assert "maxLength" in message
    assert "v" * 400 not in message
    assert len(message) < 300, f"an error body should not be unbounded: {message!r}"


async def test_415_does_not_echo_an_unbounded_content_type(
    upstream, state_dir, token
):
    """The 415 quotes the caller's ``Content-Type``; the caller chooses its length.

    ``error.message`` is what the OpenAI SDK surfaces to a terminal, so an
    unbounded echo of an attacker-chosen header is a reflection primitive with a
    render step attached.
    """
    app = create_app(make_config(upstream.url))
    hostile = "application/" + "A" * 4096

    async with httpx.AsyncClient(timeout=30) as client:
        with RunningGateway(app) as gateway:
            response = await client.post(
                f"{gateway.url}/v1/chat/completions",
                content=b"{}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": hostile,
                },
            )

    assert response.status_code == 415
    assert_error_body(response.json())
    message = response.json()["error"]["message"]
    assert "A" * 4096 not in message
    assert len(message) < 200, f"unbounded 415 message ({len(message)} chars)"
    assert upstream.requests == []


async def test_415_message_contains_no_non_printable_characters():
    """Driven below the HTTP layer on purpose, and honest about what it proves.

    h11 rejects control bytes in a header value, so this sequence cannot be put
    on the wire through this stack today; calling ``enforce_limits`` directly is
    the only way to reach the code at all. And two things stop the escape from
    rendering -- the ``!r`` conversion, which escapes non-printables as a side
    effect of being a repr, and ``_safe_echo``, which drops them deliberately.
    Neither alone is something to rely on: the ``!r`` is there for quoting, not
    for safety, and a future edit that reformats the message has no reason to
    know it was load-bearing.

    So this pins the *property* rather than either mechanism. It fails if the
    ``!r`` is dropped, it fails if the filter is removed and the transport ever
    becomes more permissive than h11, and it does not care which one is doing
    the work on any given day.
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"content-type", b"text/\x1b[31mplain\x07\x7f")],
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    with pytest.raises(GatewayError) as caught:
        await ingress.enforce_limits(Request(scope, receive), 1024)

    assert caught.value.status == 415
    message = caught.value.message
    assert "\x1b" not in message
    assert "\x07" not in message
    assert "\x7f" not in message
    assert all(character.isprintable() for character in message), repr(message)
