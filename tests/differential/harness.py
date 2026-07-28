"""The two-reduction differential harness: direct versus through the gateway.

R5 says streaming and tool-call behavior must be identical to talking to the
target provider directly. This module is what turns that sentence into a
falsifiable comparison. Each fixture is fetched twice from the same
``MockUpstream`` -- once by an HTTP client talking straight to it, once through a
gateway configured to forward to it -- and the two results are required to agree
under **two** reductions, because each catches what the other cannot.

``byte_reduction``
    Concatenates the raw response bytes and rewrites only ``id`` and ``created``,
    the two fields that legitimately vary per response. This is what catches a
    *dropped* frame -- most importantly the final usage-only chunk, which a relay
    can silently swallow while every assembled-JSON comparison still passes. It
    also catches reordering, truncation, and a merged frame terminator. Nothing
    else is rewritten: each additional rewrite is a class of divergence the
    harness stops detecting.

``semantic_reduction``
    Assembles ``tool_calls`` by ``index``, concatenates ``function.arguments``
    across frames, ``json.loads`` the result, and reports ``finish_reason`` and
    ``usage``. This is what makes a byte difference *explicable*: it separates
    "reframed but semantically identical" from "corrupted". A relay that
    helpfully reassembled six argument fragments into one frame would keep this
    reduction equal while the byte reduction went red -- and that difference is
    the diagnosis.

**What byte_reduction deliberately does not see.** HTTP chunk boundaries. The
transport below the relay is free to redraw them -- that is a property of TCP and
of the ASGI server, not of the gateway -- so the comparison is on the
concatenated body. Whether *frame* boundaries survived is exactly what the
concatenation does prove, because an SSE frame terminator is bytes.

**The gateway run drives a real supervised sidecar.** ``supervisor.start()``
spawns ``python -m hermes_auto.gateway.main`` detached, exactly as
``hermes auto start`` does, and the request crosses a real loopback socket into
real uvicorn. Neither in-process shortcut can stand in for this:
``starlette.testclient.TestClient`` buffers the whole response into a
``BytesIO`` before returning it, and ``httpx.ASGITransport`` joins every body
part with ``b"".join``. Both erase framing, and framing is the thing under test.

**Signature note.** The plan specifies ``run_direct(fixture)`` and
``run_through_gateway(fixture)``. Both take a second ``Deployment`` argument
here: a harness function has to know *which* mock and *which* sidecar it is
driving, and the fuzzer stands several fixtures against one deployment. A module
global would be the alternative and it would make the fuzzer unsafe to
parallelise. The reductions keep the specified one-argument shape.

**Return shape note.** ``run_direct``/``run_through_gateway`` return a
:class:`RunResult` rather than a bare ``list[bytes]``. The plan's own edge cases
require it: "error fixtures -- compare status, body, and headers" and
"midstream_disconnect -- both runs must fail in the same way" are not
expressible in a byte list. ``RunResult.frames`` is the ``list[bytes]``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import pathlib
import re
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

import httpx

from hermes_auto import supervisor
from hermes_auto.gateway.admin import ADMIN_TOKEN_FILENAME
from hermes_auto.gateway.upstream import HOP_BY_HOP_HEADERS
from hermes_auto.state.runtime import read_runtime
from tests.integration.mock_upstream import (
    FIXTURES,
    MockUpstream,
    fixture_bytes,
    split_frames,
)

__all__ = [
    "Deployment",
    "RunResult",
    "assert_identical",
    "baseline_reductions",
    "byte_reduction",
    "chat_body",
    "deployment",
    "describe_difference",
    "is_streaming_fixture",
    "run_direct",
    "run_through_gateway",
    "semantic_reduction",
]

#: Fixtures the mock replays as a non-SSE JSON body with a Content-Length.
NON_STREAMING_FIXTURES: frozenset[str] = frozenset(
    {"error_401", "error_429", "error_context_length"}
)

#: The one fixture whose upstream response stops without its terminating chunk.
TRUNCATED_FIXTURES: frozenset[str] = frozenset({"midstream_disconnect"})

#: Generous: a sidecar spawn on a cold Windows filesystem is not fast.
STARTUP_TIMEOUT_SECONDS = 90.0
STOP_TIMEOUT_SECONDS = 45.0
CLIENT_TIMEOUT_SECONDS = 60.0

#: Headers whose relay is not asserted. Hop-by-hop describe a single connection
#: and are correctly dropped; ``date`` is minted by uvicorn and by nothing in the
#: mock, so requiring it to match would assert a clock.
_UNCOMPARED_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS | {"date", "server"}

# The frame-leading ``"id"`` only. Anchoring to the start of a ``data:`` line
# keeps tool-call ids -- which appear as ``"id"`` deeper inside the same frame --
# under full comparison. A relay that corrupted a tool-call id must not be able
# to hide behind the response-id rewrite.
_FRAME_ID = re.compile(rb'(?m)^(data: \{"id":)"[^"]*"')
_CREATED = re.compile(rb'(?m)^(data: \{[^\n\r]*?"created":)\d+')

_DATA_LINE = re.compile(rb"(?m)^data: ")


def is_streaming_fixture(fixture: str) -> bool:
    """True when the mock replays *fixture* as SSE rather than as a JSON body."""
    if fixture not in FIXTURES:
        raise KeyError(f"unknown fixture {fixture!r}")
    return fixture not in NON_STREAMING_FIXTURES


# ---------------------------------------------------------------------------
# Reduction 1: bytes
# ---------------------------------------------------------------------------


def byte_reduction(frames: Iterable[bytes]) -> bytes:
    """Concatenate *frames* and rewrite only ``id`` and ``created``.

    Two rewrites and no others. ``id`` and ``created`` are the two fields an
    OpenAI-compatible provider legitimately varies between two otherwise
    identical responses; everything else -- frame order, frame count, terminator
    spelling, whitespace, key order, the presence of the usage-only final chunk
    -- stays inside the comparison.

    Both patterns are anchored to the start of a ``data:`` line so they cannot
    reach a nested ``"id"`` (a tool-call id) or a nested ``"created"``. A frame
    whose key order defeats the anchor is simply left alone, which makes the
    comparison stricter rather than looser.

    Args:
        frames: The response byte chunks, in arrival order.

    Returns:
        The reduced body. Equal reductions mean the two paths put the same
        bytes on the wire, up to the two permitted fields.
    """
    payload = b"".join(frames)
    payload = _FRAME_ID.sub(rb'\1"<id>"', payload)
    return _CREATED.sub(rb"\g<1>0", payload)


# ---------------------------------------------------------------------------
# Reduction 2: semantics
# ---------------------------------------------------------------------------


def _blank_choice() -> dict[str, Any]:
    return {
        "role": None,
        "content": "",
        "reasoning_content": "",
        "refusal": None,
        "finish_reason": None,
        "tool_calls": {},
    }


def _absorb_tool_calls(choice: dict[str, Any], deltas: Sequence[Any]) -> None:
    """Fold one frame's ``tool_calls`` deltas into the accumulating choice.

    Keyed on ``index`` and on nothing else. ``parallel_tool_calls`` deliberately
    emits two calls with the same ``function.name``, so name is not a key; ids
    arrive only on the opening delta, so id is not a key either. A relay -- or a
    reducer -- that assembled on either one reassembles the wrong arguments onto
    the wrong call.
    """
    for delta in deltas:
        if not isinstance(delta, dict):
            choice.setdefault("malformed_tool_call_deltas", []).append(repr(delta))
            continue
        index = delta.get("index")
        slot = choice["tool_calls"].setdefault(
            index, {"id": None, "type": None, "name": None, "arguments": ""}
        )
        if delta.get("id") is not None:
            slot["id"] = delta["id"]
        if delta.get("type") is not None:
            slot["type"] = delta["type"]
        function = delta.get("function")
        if isinstance(function, dict):
            if function.get("name") is not None:
                slot["name"] = function["name"]
            fragment = function.get("arguments")
            if isinstance(fragment, str):
                # Concatenation in arrival order is the whole point:
                # `fragmented_arguments` splits the key "arguments" as
                # `"argu` / `ments"`, so no fragment parses alone.
                slot["arguments"] += fragment


def semantic_reduction(frames: Iterable[bytes]) -> dict[str, Any]:
    """Reassemble *frames* into a comparable structure.

    For an SSE body: every ``data:`` event is parsed, per-choice ``content`` and
    ``reasoning_content`` are concatenated, ``tool_calls`` are assembled by
    ``index`` with ``function.arguments`` concatenated and then ``json.loads``ed,
    and ``finish_reason`` plus ``usage`` are carried through.

    For a non-SSE body (the three error fixtures): the parsed JSON document.

    **Deliberately blind to framing.** The event count, the number and position
    of comment keepalives, and the frame terminator spelling are *not* in the
    returned structure. That is what makes this reduction useful next to the byte
    one: a relay that reassembled six argument fragments into a single frame, or
    swallowed a ``: ping``, or rewrote CRLF to LF, turns the byte comparison red
    and leaves this one green -- and that pair of results *is* the diagnosis
    "reframed, not corrupted". Putting a frame count in here would make both
    reductions say the same thing and cost the harness its ability to explain a
    difference.

    A frame that does not parse -- the truncated tail of
    ``midstream_disconnect`` -- is recorded verbatim under ``unparsed`` rather
    than dropped. Dropping it would make a truncated stream reduce like a
    complete one, which is the divergence this fixture exists to expose.

    Args:
        frames: The response byte chunks, in arrival order.

    Returns:
        A plain dict of JSON-comparable values. Never raises on malformed input:
        a reducer that raises turns a divergence into an error and loses the
        diagnosis.
    """
    payload = b"".join(frames)

    if not _DATA_LINE.search(payload):
        return {
            "kind": "body",
            "length": len(payload),
            "json": _loads(payload.decode("utf-8", "surrogatepass")),
        }

    result: dict[str, Any] = {
        "kind": "stream",
        "object": None,
        "model": None,
        "unparsed": [],
        "done_sentinel": False,
        "events_after_done": [],
        "choices": {},
        "usage": None,
    }

    seen_done = False
    for frame in split_frames(payload):
        text = frame.decode("utf-8", "surrogatepass")
        for line in text.splitlines():
            if not line.strip():
                continue
            if line.startswith(":"):
                # An SSE comment keepalive. Counted by neither reduction on
                # purpose -- see this function's docstring.
                continue
            if not line.startswith("data: "):
                result["unparsed"].append(line)
                continue
            data = line[len("data: ") :]
            if data == "[DONE]":
                result["done_sentinel"] = True
                seen_done = True
                continue
            if seen_done:
                # Content after the terminator is a semantic fact, not framing.
                result["events_after_done"].append(line)
            event = _loads(data)
            if not isinstance(event, dict):
                result["unparsed"].append(line)
                continue
            _absorb_event(result, event)

    result["choices"] = {
        str(index): _finish_choice(choice)
        for index, choice in sorted(result["choices"].items(), key=lambda kv: str(kv[0]))
    }
    return result


def _absorb_event(result: dict[str, Any], event: dict[str, Any]) -> None:
    if result["object"] is None:
        result["object"] = event.get("object")
    if result["model"] is None:
        result["model"] = event.get("model")
    if event.get("usage") is not None:
        result["usage"] = event["usage"]

    choices = event.get("choices")
    if not isinstance(choices, list):
        result["unparsed"].append(f"choices is {type(choices).__name__}")
        return

    for entry in choices:
        if not isinstance(entry, dict):
            result["unparsed"].append(f"choice is {type(entry).__name__}")
            continue
        choice = result["choices"].setdefault(entry.get("index"), _blank_choice())
        if entry.get("finish_reason") is not None:
            choice["finish_reason"] = entry["finish_reason"]
        delta = entry.get("delta")
        if not isinstance(delta, dict):
            continue
        if delta.get("role") is not None:
            choice["role"] = delta["role"]
        if isinstance(delta.get("content"), str):
            choice["content"] += delta["content"]
        if isinstance(delta.get("reasoning_content"), str):
            choice["reasoning_content"] += delta["reasoning_content"]
        if delta.get("refusal") is not None:
            choice["refusal"] = delta["refusal"]
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            _absorb_tool_calls(choice, tool_calls)


def _finish_choice(choice: dict[str, Any]) -> dict[str, Any]:
    """Parse each assembled argument string, recording failure rather than raising."""
    calls: dict[str, Any] = {}
    for index, slot in sorted(
        choice["tool_calls"].items(), key=lambda kv: str(kv[0])
    ):
        arguments = slot["arguments"]
        parsed: Any
        parses = True
        try:
            parsed = json.loads(arguments) if arguments else None
        except ValueError as exc:
            parsed = None
            parses = False
            slot = {**slot, "arguments_error": type(exc).__name__}
        calls[str(index)] = {
            **slot,
            "arguments_json": parsed,
            "arguments_parse_ok": parses,
        }
    return {**choice, "tool_calls": calls}


def _loads(text: str | bytes) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        return {"__unparsed__": repr(text)[:400], "__error__": type(exc).__name__}


# ---------------------------------------------------------------------------
# The two runs
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RunResult:
    """One side of the comparison."""

    #: HTTP status, or None when the request never produced a response.
    status: int | None
    #: Response headers as ordered lowercase pairs; duplicates preserved.
    headers: tuple[tuple[str, str], ...]
    #: The response byte chunks, in arrival order. Chunk *boundaries* are not
    #: comparable across the two paths; their concatenation is.
    frames: list[bytes]
    #: Exception class name when the read ended in a transport error.
    failure: str | None

    @property
    def body(self) -> bytes:
        return b"".join(self.frames)

    def comparable_headers(self) -> dict[str, str]:
        return {
            name: value
            for name, value in self.headers
            if name not in _UNCOMPARED_HEADERS
        }


@dataclasses.dataclass
class Deployment:
    """One live install: a scripted upstream plus a supervised sidecar in front.

    Holds two long-lived clients rather than opening one per request. Connection
    reuse is what the OpenAI SDK does, so it is the more faithful measurement --
    and the fuzzer issues several hundred requests, each of which would otherwise
    leave two sockets in ``TIME_WAIT``.
    """

    upstream: MockUpstream
    state_dir: pathlib.Path
    config_path: pathlib.Path
    port: int
    admin_port: int
    token: str
    admin_token: str
    instance_id: str
    strict_validation: bool
    direct_client: httpx.Client = dataclasses.field(
        default_factory=lambda: httpx.Client(timeout=CLIENT_TIMEOUT_SECONDS)
    )
    gateway_client: httpx.Client = dataclasses.field(
        default_factory=lambda: httpx.Client(timeout=CLIENT_TIMEOUT_SECONDS)
    )

    def close(self) -> None:
        self.direct_client.close()
        self.gateway_client.close()

    @property
    def gateway_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def admin_url(self) -> str:
        return f"http://127.0.0.1:{self.admin_port}"

    @property
    def upstream_chat_url(self) -> str:
        return f"{self.upstream.url}/v1/chat/completions"

    @property
    def gateway_chat_url(self) -> str:
        return f"{self.gateway_url}/v1/chat/completions"


def chat_body(*, stream: bool = True, with_envelope: bool = True, **extra: Any) -> dict:
    """The request both paths send. Identical apart from the bearer token."""
    body: dict[str, Any] = {
        "model": "auto:balanced",
        "messages": [{"role": "user", "content": "differential probe"}],
        "stream": stream,
    }
    if with_envelope:
        body["_hermes_auto"] = {
            "protocol_version": 1,
            "root_session_id": "session-differential",
            "virtual_model": "auto:balanced",
            "plugin_version": "0.1.0",
        }
    body.update(extra)
    return body


def _collect(
    client: httpx.Client, url: str, *, json_body: dict, headers: dict[str, str]
) -> RunResult:
    chunks: list[bytes] = []
    failure: str | None = None
    with client.stream("POST", url, json=json_body, headers=headers) as response:
        status = response.status_code
        pairs = tuple(
            (name.lower(), value) for name, value in response.headers.multi_items()
        )
        try:
            # aiter_raw's sync twin: undecoded transport bytes, no reassembly.
            for chunk in response.iter_raw():
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            failure = type(exc).__name__
    return RunResult(status=status, headers=pairs, frames=chunks, failure=failure)


def run_direct(
    fixture: str,
    deployment: Deployment,
    *,
    rechunk: Sequence[int] | None = None,
) -> RunResult:
    """Fetch *fixture* straight from the mock, bypassing the gateway entirely."""
    deployment.upstream.script(fixture)
    deployment.upstream.set_rechunk(rechunk)
    return _collect(
        deployment.direct_client,
        deployment.upstream_chat_url,
        json_body=chat_body(stream=is_streaming_fixture(fixture)),
        headers={},
    )


def run_through_gateway(
    fixture: str,
    deployment: Deployment,
    *,
    rechunk: Sequence[int] | None = None,
) -> RunResult:
    """Fetch *fixture* through the supervised sidecar in front of the same mock."""
    deployment.upstream.script(fixture)
    deployment.upstream.set_rechunk(rechunk)
    return _collect(
        deployment.gateway_client,
        deployment.gateway_chat_url,
        json_body=chat_body(stream=is_streaming_fixture(fixture)),
        headers={"Authorization": f"Bearer {deployment.token}"},
    )


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def describe_difference(direct: bytes, relayed: bytes) -> str:
    """Name the first differing byte offset and the frame it lands in.

    A failure message that says only "bytes differ" costs an hour of bisection on
    a two-kilobyte body, and considerably more on a real one.
    """
    limit = min(len(direct), len(relayed))
    offset = limit
    for index in range(limit):
        if direct[index] != relayed[index]:
            offset = index
            break

    frames = split_frames(direct)
    frame_index = len(frames)
    frame_offset = 0
    consumed = 0
    for index, frame in enumerate(frames):
        if consumed + len(frame) > offset:
            frame_index = index
            frame_offset = offset - consumed
            break
        consumed += len(frame)

    lines = [
        f"first difference at byte offset {offset} of "
        f"{len(direct)} (direct) / {len(relayed)} (relayed)",
        f"direct frame count {len(frames)}, relayed frame count "
        f"{len(split_frames(relayed))}",
    ]
    if frame_index < len(frames):
        lines.append(
            f"offset lands in direct frame index {frame_index} at frame-relative "
            f"byte {frame_offset}"
        )
        lines.append(f"  direct  frame: {frames[frame_index]!r}")
        relayed_frames = split_frames(relayed)
        if frame_index < len(relayed_frames):
            lines.append(f"  relayed frame: {relayed_frames[frame_index]!r}")
        else:
            lines.append("  relayed frame: <relayed body has no frame at that index>")
    else:
        longer, label = (
            (direct, "direct") if len(direct) > len(relayed) else (relayed, "relayed")
        )
        lines.append(
            f"bodies agree for {offset} bytes; {label} continues with "
            f"{longer[offset : offset + 200]!r}"
        )
    lines.append(f"  direct  window: {direct[max(0, offset - 60) : offset + 60]!r}")
    lines.append(f"  relayed window: {relayed[max(0, offset - 60) : offset + 60]!r}")
    return "\n".join(lines)


def assert_identical(
    fixture: str,
    deployment: Deployment,
    *,
    rechunk: Sequence[int] | None = None,
) -> tuple[RunResult, RunResult]:
    """Run *fixture* both ways and require both reductions to agree.

    Also compares status, the transport-failure outcome, and every response
    header the direct caller saw that is not hop-by-hop. Returns both results so
    a caller can make fixture-specific assertions on top.

    Raises:
        AssertionError: naming the first differing frame index and byte offset.
    """
    direct = run_direct(fixture, deployment, rechunk=rechunk)
    relayed = run_through_gateway(fixture, deployment, rechunk=rechunk)

    assert relayed.status == direct.status, (
        f"{fixture}: status {relayed.status} through the gateway, "
        f"{direct.status} direct"
    )

    reduced_direct = byte_reduction(direct.frames)
    reduced_relayed = byte_reduction(relayed.frames)
    assert reduced_relayed == reduced_direct, (
        f"{fixture}: byte reduction differs.\n"
        + describe_difference(reduced_direct, reduced_relayed)
    )

    semantic_direct = semantic_reduction(direct.frames)
    semantic_relayed = semantic_reduction(relayed.frames)
    assert semantic_relayed == semantic_direct, (
        f"{fixture}: semantic reduction differs.\n"
        f"  direct : {semantic_direct}\n"
        f"  relayed: {semantic_relayed}"
    )

    assert relayed.failure == direct.failure, (
        f"{fixture}: direct read ended with {direct.failure!r} but the relayed "
        f"read ended with {relayed.failure!r}. A truncated upstream that reaches "
        f"the client as a clean 200 is a divergence, not a repair."
    )

    direct_headers = direct.comparable_headers()
    relayed_headers = relayed.comparable_headers()
    for name, value in direct_headers.items():
        assert relayed_headers.get(name) == value, (
            f"{fixture}: header {name!r} was {value!r} direct and "
            f"{relayed_headers.get(name)!r} through the gateway"
        )

    return direct, relayed


# ---------------------------------------------------------------------------
# Standing the deployment up
# ---------------------------------------------------------------------------


def _write_config(
    path: pathlib.Path, upstream_url: str, *, strict_validation: bool
) -> None:
    path.write_text(
        "auto_router:\n"
        "  gateway:\n"
        '    url: "http://127.0.0.1:0"\n'
        "    port: 0\n"
        "    admin_port: 0\n"
        f"    startup_timeout_seconds: {int(STARTUP_TIMEOUT_SECONDS)}\n"
        f"    strict_validation: {'true' if strict_validation else 'false'}\n"
        "  upstream:\n"
        f'    base_url: "{upstream_url}/v1"\n'
        '    model: "fixed-target"\n'
        '    credential_ref: "none"\n',
        encoding="utf-8",
    )


@contextlib.contextmanager
def _environment(**values: str) -> Iterator[None]:
    """Set environment variables for the block, restoring them afterwards.

    Not ``monkeypatch``: this context manager is used from module-scoped
    fixtures, and pytest's ``monkeypatch`` is function-scoped.
    """
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, was in previous.items():
            if was is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = was


@contextlib.contextmanager
def deployment(
    root: pathlib.Path, *, strict_validation: bool = False
) -> Iterator[Deployment]:
    """Stand up a mock upstream and a real supervised sidecar in front of it.

    ``HERMES_AUTO_STATE_DIR`` outranks configuration by design
    (``state/paths.py``), so it is set here rather than written into the config
    file -- and for the same reason only **one** deployment can be live in a
    process at a time. Plan 02-07 hit this: a second state directory is not
    reachable from inside one interpreter, and its first attempt silently wrote
    into the first install's directory. Two deployments therefore run
    sequentially, never nested.

    Teardown always stops the sidecar, including after a failure: a detached
    process outliving a failed test holds a port and breaks every test after it.
    """
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.yaml"

    with MockUpstream() as upstream:
        _write_config(config_path, upstream.url, strict_validation=strict_validation)
        with _environment(
            HERMES_AUTO_STATE_DIR=str(state),
            HERMES_AUTO_CONFIG=str(config_path),
            HERMES_AUTO_TEST_EPHEMERAL="1",
        ):
            try:
                started = supervisor.start(timeout=STARTUP_TIMEOUT_SECONDS)
                record = read_runtime(None)
                if record is None:  # pragma: no cover - start would have raised
                    raise RuntimeError("the gateway started but wrote no runtime file")
                if record.admin_port == 0:
                    raise RuntimeError(
                        "the sidecar recorded admin_port 0, the sentinel for 'no "
                        "admin listener'. The token-scope assertions need both "
                        "listeners actually running."
                    )
                live = Deployment(
                    upstream=upstream,
                    state_dir=state,
                    config_path=config_path,
                    port=record.port,
                    admin_port=record.admin_port,
                    token=_read_secret(state / "token"),
                    admin_token=_read_secret(state / ADMIN_TOKEN_FILENAME),
                    instance_id=started.instance_id or record.instance_id,
                    strict_validation=strict_validation,
                )
                try:
                    yield live
                finally:
                    live.close()
            finally:
                with contextlib.suppress(Exception):
                    supervisor.stop(timeout=STOP_TIMEOUT_SECONDS)


def _read_secret(path: pathlib.Path, *, timeout: float = 15.0) -> str:
    """Read a token file, waiting briefly for the sidecar's lifespan to mint it.

    The inference token is minted by ``app.py``'s lifespan and the admin token by
    ``admin.py``'s, both of which run before ``/healthz`` answers -- but the
    write and this read are in different processes, so a short wait is cheaper
    than an intermittent failure.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{path} was never written by the sidecar")
        time.sleep(0.05)


def baseline_reductions(fixture: str) -> tuple[bytes, dict[str, Any]]:
    """Both reductions of the fixture's on-disk bytes.

    The fixed point the fuzzer measures every re-split against: if re-chunking
    changed content, this is what it would stop matching.
    """
    payload = fixture_bytes(fixture)
    return byte_reduction([payload]), semantic_reduction([payload])
