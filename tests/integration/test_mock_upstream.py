"""Tests that establish the oracle itself is sound, before anything is measured against it.

Plans 02-04, 02-08, and 02-09 all treat ``MockUpstream`` plus the corpus in
``tests/fixtures/sse/`` as ground truth for the claim "the gateway behaves
identically to talking to the provider directly". A differential harness is only
as good as its oracle, so every property those plans will rely on is pinned here:

* replay is byte-identical, per fixture, for all twelve;
* re-chunking changes framing and never content -- the invariant plan 02-08's
  ``hypothesis`` fuzzer asserts, exercised here including a split placed *inside*
  a multi-byte UTF-8 codepoint;
* the truncation fixture produces a transport error, not a clean short body;
* the corpus itself still holds the adversarial properties it was built for.

The client is stdlib ``http.client`` on purpose. ``httpx`` arrives with plan
02-04, and an oracle whose own tests depend on the HTTP stack under test cannot
independently confirm what went over the wire.
"""

from __future__ import annotations

import http.client
import json
import pathlib
import re
import shutil
import subprocess
import time
from typing import Any

import pytest

from hermes_auto.gateway.schemas import build_validator, load_schemas
from tests.integration.mock_upstream import (
    FIXTURE_DIR,
    FIXTURES,
    MockUpstream,
    UnknownFixtureError,
    fixture_bytes,
    split_frames,
)

# Windows allocates a fresh console window for a console-subsystem child when the
# parent has no console of its own. CREATE_NO_WINDOW suppresses it; it is absent on
# POSIX, hence getattr.
_NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SSE_CHUNK_SCHEMA_ID = (
    "https://hermes-auto-router.dev/schema/wire/sse-stream-contract.v1.json"
)

# Hoisted once at import, not rebuilt per frame. ``validate()`` re-runs
# ``check_schema`` on every call, which dominates its cost by roughly two orders
# of magnitude; this module validates a few hundred frames.
_CHUNK_VALIDATOR = build_validator(SSE_CHUNK_SCHEMA_ID, load_schemas())

STREAMING_FIXTURES = sorted(n for n, s in FIXTURES.items() if s.chunked)
ERROR_FIXTURES = sorted(n for n, s in FIXTURES.items() if not s.chunked)
ALL_FIXTURES = sorted(FIXTURES)

_TERMINATORS = (b"\r\n\r\n", b"\n\n")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def post(
    upstream: MockUpstream,
    body: dict[str, Any] | None = None,
    path: str = "/v1/chat/completions",
) -> tuple[int, dict[str, str], bytes]:
    """POST to *upstream* and return ``(status, headers, raw body bytes)``."""
    conn = http.client.HTTPConnection("127.0.0.1", upstream.port, timeout=10)
    try:
        conn.request(
            "POST",
            path,
            body=json.dumps(body if body is not None else {"model": "test-model-a"}),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def complete_data_payloads(body: bytes) -> list[bytes]:
    """Return the JSON payload of every *complete* ``data:`` frame in *body*.

    Comment keepalives (``: ping``), the ``[DONE]`` sentinel, and a trailing
    unterminated fragment are all excluded. Excluding the unterminated fragment
    is what makes ``midstream_disconnect`` yield exactly its two valid frames
    rather than a parse error.
    """
    payloads: list[bytes] = []
    for piece in split_frames(body):
        if not piece.endswith(_TERMINATORS):
            continue  # trailing truncated fragment; not a frame yet
        line = piece.rstrip(b"\r\n")
        if not line.startswith(b"data: "):
            continue  # SSE comment / keepalive
        payload = line[len(b"data: ") :]
        if payload == b"[DONE]":
            continue
        payloads.append(payload)
    return payloads


def tool_call_argument_fragments(body: bytes) -> list[str]:
    """Every non-empty ``function.arguments`` fragment, in arrival order."""
    fragments: list[str] = []
    for payload in complete_data_payloads(body):
        for choice in json.loads(payload.decode("utf-8"))["choices"]:
            for call in choice["delta"].get("tool_calls", []):
                argument = call.get("function", {}).get("arguments")
                if argument:
                    fragments.append(argument)
    return fragments


# --------------------------------------------------------------------------
# corpus integrity -- properties of the bytes on disk
# --------------------------------------------------------------------------


def test_corpus_and_registry_agree_exactly() -> None:
    """Every ``.txt`` on disk is registered, and every registered name exists.

    Checked as a set rather than a count. A count passes when a fixture is added
    at the same moment another is renamed, which is exactly the silent swap that
    would leave a later plan measuring against a corpus it did not intend.
    """
    on_disk = {p.stem for p in FIXTURE_DIR.glob("*.txt")}
    assert on_disk == set(FIXTURES)
    assert len(on_disk) == 12


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_fixture_files_contain_no_commentary(name: str) -> None:
    """``.txt`` files are payload bytes only; documentation lives in the README.

    A ``#`` header or a stray blank line would become part of the byte sequence
    the differential harness compares, so the format rule is enforced rather
    than merely written down.
    """
    raw = fixture_bytes(name)
    assert raw, f"{name} is empty"
    if FIXTURES[name].chunked:
        for piece in split_frames(raw):
            line = piece.rstrip(b"\r\n")
            assert line.startswith((b"data: ", b": ")), (
                f"{name}: non-SSE line {line[:60]!r}"
            )
    else:
        json.loads(raw.decode("utf-8"))  # errors are bare JSON envelopes


@pytest.mark.parametrize("name", ERROR_FIXTURES)
def test_error_fixtures_have_no_trailing_newline(name: str) -> None:
    """An editor "fixing" the file by adding a final newline must fail the suite.

    An OpenAI-compatible upstream sends the error envelope with no trailing
    newline, and ``Content-Length`` is derived from these bytes.
    """
    assert not fixture_bytes(name).endswith(b"\n")


def test_corpus_carries_no_credentials_or_real_model_ids() -> None:
    """No real secret, real provider model id, or developer path in the corpus.

    Broader than a bare credential grep: a plausible-looking real model id in a
    fixture is how a synthetic corpus quietly acquires a dependency on one
    vendor's behavior.
    """
    forbidden = re.compile(
        r"sk-[A-Za-z0-9]{10}|ghp_[A-Za-z0-9]{10}|AKIA[0-9A-Z]{16}"
        r"|gpt-[0-9]|claude-[0-9]|gemini-[0-9]|llama-?[0-9]|mistral-|command-r"
        r"|[A-Za-z]:\\\\Users|/home/[a-z]|/Users/[a-z]",
        re.IGNORECASE,
    )
    for name in ALL_FIXTURES:
        text = fixture_bytes(name).decode("utf-8", errors="replace")
        assert not forbidden.search(text), f"{name} contains a forbidden token"
        assert "test-model-a" in text or name.startswith("error_")


def test_git_does_not_translate_fixture_bytes() -> None:
    """The corpus is marked binary to git, so a checkout cannot rewrite it.

    This repository has ``core.autocrlf=true``. Without the ``-text`` attribute
    scoped to this directory, git rewrites every ``\\n`` frame terminator to
    ``\\r\\n`` on checkout, and the frame boundaries -- the thing the whole
    corpus exists to pin -- differ from what was committed. That failure is
    invisible in the authoring working tree and only appears in CI or on a fresh
    clone, so it is asserted here rather than trusted.
    """
    if shutil.which("git") is None:  # pragma: no cover - git is present in CI
        pytest.skip("git unavailable")
    result = subprocess.run(
        ["git", "check-attr", "text", "--"]
        + [str(FIXTURE_DIR / f"{n}.txt") for n in ALL_FIXTURES],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).resolve().parent.parent.parent,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    if result.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip(f"git check-attr unavailable: {result.stderr.strip()}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 12
    for line in lines:
        assert line.endswith(": text: unset"), line


# --------------------------------------------------------------------------
# the adversarial fixtures, asserted on their actual bytes
# --------------------------------------------------------------------------


def test_fragmented_arguments_concatenate_but_never_parse_alone() -> None:
    """The property that makes this fixture catch tool-call reframing.

    A fixture whose fragments each happen to be valid JSON proves nothing: a
    relay could reorder or re-frame them and the assembled result would still
    parse. The catch requires that *only* the concatenation in arrival order is
    valid.
    """
    fragments = tool_call_argument_fragments(fixture_bytes("fragmented_arguments"))
    assert len(fragments) >= 4, fragments

    for fragment in fragments:
        with pytest.raises(ValueError):
            json.loads(fragment)

    assert json.loads("".join(fragments)) == {
        "path": "/tmp/synthetic.txt",
        "arguments": ["--dry-run"],
        "mode": "read",
    }
    # The split named in 02-CONTEXT.md: a JSON key cut as "argu" / "ments".
    assert any(f.endswith('"argu') for f in fragments), fragments


def test_fragmented_utf8_splits_a_codepoint_across_a_frame_boundary() -> None:
    """U+1F600 is delivered as two lone surrogate halves in adjacent frames.

    This is what OpenAI-compatible upstreams actually emit, and it is the case
    that breaks a relay which encodes each delta to UTF-8 independently: neither
    half is encodable on its own.
    """
    contents = [
        choice["delta"]["content"]
        for payload in complete_data_payloads(fixture_bytes("fragmented_utf8"))
        for choice in json.loads(payload.decode("utf-8"))["choices"]
        if choice["delta"].get("content") is not None
    ]
    high = [
        i for i, c in enumerate(contents) if len(c) == 1 and 0xD800 <= ord(c) <= 0xDBFF
    ]
    assert high, f"no lone high surrogate frame in {contents!r}"

    first, second = contents[high[0]], contents[high[0] + 1]
    assert 0xDC00 <= ord(second) <= 0xDFFF, "halves are not in adjacent frames"

    for half in (first, second):
        with pytest.raises(UnicodeEncodeError):
            half.encode("utf-8")

    joined = (first + second).encode("utf-16", "surrogatepass").decode("utf-16")
    assert ord(joined) == 0x1F600
    assert joined.encode("utf-8") == b"\xf0\x9f\x98\x80"


def test_fragmented_utf8_contains_raw_multibyte_sequences() -> None:
    """Raw 2-, 3-, and 4-byte sequences exist, so a byte split can land inside one.

    Without a real multi-byte sequence in the payload there is no byte offset for
    ``set_rechunk`` to cut mid-codepoint, and the re-chunking fuzzer would only
    ever be exercising ASCII.
    """
    raw = fixture_bytes("fragmented_utf8")
    for sequence in (b"\xc3\xa9", b"\xe6\x97\xa5", b"\xf0\x9d\x84\x9e"):
        assert sequence in raw, sequence


def test_midstream_disconnect_fixture_is_genuinely_truncated() -> None:
    """Two complete frames, then a fragment that cannot parse, and no ``[DONE]``."""
    raw = fixture_bytes("midstream_disconnect")
    assert b"[DONE]" not in raw
    assert not raw.endswith(_TERMINATORS)
    assert len(complete_data_payloads(raw)) == 2

    tail = split_frames(raw)[-1]
    assert not tail.endswith(_TERMINATORS)
    with pytest.raises(ValueError):
        json.loads(tail[len(b"data: ") :].decode("utf-8"))


def test_empty_deltas_and_keepalives_carries_both() -> None:
    """An empty ``delta`` and an SSE comment keepalive, on CRLF terminators.

    The CRLF framing is deliberate: the SSE grammar permits CR, LF, or CRLF, and
    a relay that hardcodes ``b"\\n\\n"`` as the frame separator breaks only here.
    """
    raw = fixture_bytes("empty_deltas_and_keepalives")
    assert b"\r\n\r\n" in raw
    assert b": ping" in raw

    deltas = [
        choice["delta"]
        for payload in complete_data_payloads(raw)
        for choice in json.loads(payload.decode("utf-8"))["choices"]
    ]
    assert {} in deltas, deltas

    # A usage-shaped final chunk with no choices at all must also be present,
    # since that is the frame a naive relay is most likely to drop.
    assert any(
        json.loads(p.decode("utf-8"))["choices"] == []
        for p in complete_data_payloads(raw)
    )


def test_usage_only_final_chunk_has_empty_choices_and_usage() -> None:
    payloads = complete_data_payloads(fixture_bytes("usage_only_final_chunk"))
    final = json.loads(payloads[-1].decode("utf-8"))
    assert final["choices"] == []
    assert final["usage"]["total_tokens"] == 15


def test_parallel_tool_calls_are_distinguishable_only_by_index() -> None:
    """Both calls share a function name, so ``index`` is the only assembly key."""
    assembled: dict[int, str] = {}
    names: set[str] = set()
    for payload in complete_data_payloads(fixture_bytes("parallel_tool_calls")):
        for choice in json.loads(payload.decode("utf-8"))["choices"]:
            for call in choice["delta"].get("tool_calls", []):
                function = call.get("function", {})
                if "name" in function:
                    names.add(function["name"])
                assembled[call["index"]] = assembled.get(call["index"], "") + function.get(
                    "arguments", ""
                )
    assert len(names) == 1, "differing names would make index redundant"
    assert sorted(assembled) == [0, 1]
    assert json.loads(assembled[0]) == {"path": "/tmp/synthetic-a.txt"}
    assert json.loads(assembled[1]) == {"path": "/tmp/synthetic-b.txt"}


def test_refusal_has_non_null_refusal_and_content_filter() -> None:
    payloads = complete_data_payloads(fixture_bytes("refusal"))
    refusals = [
        c["delta"]["refusal"]
        for p in payloads
        for c in json.loads(p.decode("utf-8"))["choices"]
        if c["delta"].get("refusal") is not None
    ]
    finishes = {
        c.get("finish_reason")
        for p in payloads
        for c in json.loads(p.decode("utf-8"))["choices"]
    }
    assert refusals
    assert "content_filter" in finishes


def test_text_stream_carries_a_provider_reasoning_field() -> None:
    """design.md 20.3 lists provider reasoning fields; ``delta`` allows unknowns.

    This doubles as the passthrough probe: an unmodelled field that must survive
    the gateway verbatim.
    """
    deltas = [
        c["delta"]
        for p in complete_data_payloads(fixture_bytes("text_stream"))
        for c in json.loads(p.decode("utf-8"))["choices"]
    ]
    assert any("reasoning_content" in d for d in deltas), deltas


# --------------------------------------------------------------------------
# schema conformance
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", STREAMING_FIXTURES)
def test_every_streaming_frame_validates_against_the_frozen_contract(name: str) -> None:
    """Each complete ``data:`` frame satisfies ``sse-stream-contract.v1``.

    Validated through the module-level hoisted validator, never ``validate()``.
    """
    payloads = complete_data_payloads(fixture_bytes(name))
    assert payloads, f"{name} produced no frames -- an empty fixture would pass vacuously"
    for payload in payloads:
        _CHUNK_VALIDATOR.validate(json.loads(payload.decode("utf-8")))


@pytest.mark.parametrize("name", ERROR_FIXTURES)
def test_error_fixtures_are_openai_shaped_envelopes(name: str) -> None:
    envelope = json.loads(fixture_bytes(name).decode("utf-8"))
    assert set(envelope) == {"error"}
    assert {"message", "type", "code"} <= set(envelope["error"])


# --------------------------------------------------------------------------
# replay behavior
# --------------------------------------------------------------------------


@pytest.fixture()
def upstream() -> Any:
    with MockUpstream() as server:
        yield server


@pytest.mark.parametrize("name", sorted(set(ALL_FIXTURES) - {"midstream_disconnect"}))
def test_replay_is_byte_identical(upstream: MockUpstream, name: str) -> None:
    """What comes off the wire equals the file on disk, byte for byte."""
    upstream.script(name)
    status, headers, body = post(upstream)
    assert status == FIXTURES[name].status
    assert body == fixture_bytes(name)
    for key, value in FIXTURES[name].headers:
        assert headers.get(key) == value


def test_responses_carry_no_clock_derived_headers(upstream: MockUpstream) -> None:
    """No ``Date`` header, so a response is reproducible byte for byte."""
    upstream.script("text_stream")
    _, headers, _ = post(upstream)
    assert "Date" not in headers
    assert "Server" not in headers


def test_error_429_sends_retry_after(upstream: MockUpstream) -> None:
    """A dropped ``Retry-After`` turns a recoverable throttle into an opaque failure."""
    upstream.script("error_429")
    status, headers, _ = post(upstream)
    assert status == 429
    assert headers["Retry-After"] == "20"


def test_midstream_disconnect_raises_rather_than_short_reading(
    upstream: MockUpstream,
) -> None:
    """The client must see a transport error, not a clean truncated body.

    A mock that returned the short body cleanly would let a gateway which
    silently swallows a dropped upstream pass the differential harness.
    """
    upstream.script("midstream_disconnect")
    with pytest.raises(http.client.IncompleteRead) as caught:
        post(upstream)
    assert caught.value.partial == fixture_bytes("midstream_disconnect")


# --------------------------------------------------------------------------
# re-chunking: the primitive plan 02-08's fuzzer drives
# --------------------------------------------------------------------------

_RECHUNK_CASES = [
    [1],  # one byte per wire chunk
    [3, 3, 3, 3, 3],
    [7, 3, 11, 29],
    [512, 1],
    [10_000],  # larger than any fixture: whole body in one chunk
]


@pytest.mark.parametrize("sizes", _RECHUNK_CASES, ids=lambda s: f"sizes{len(s)}-{s[0]}")
@pytest.mark.parametrize("name", ["text_stream", "fragmented_utf8", "fragmented_arguments"])
def test_rechunking_changes_framing_never_content(
    upstream: MockUpstream, name: str, sizes: list[int]
) -> None:
    upstream.script(name)
    upstream.set_rechunk(sizes)
    _, _, body = post(upstream)
    assert body == fixture_bytes(name)


def test_rechunk_split_inside_a_utf8_codepoint_preserves_bytes(
    upstream: MockUpstream,
) -> None:
    """The sharpest case: a wire chunk that ends mid-codepoint.

    The offset is computed from the fixture rather than hardcoded, and the test
    first *proves* the boundary is mid-codepoint by asserting the prefix does not
    decode. Without that proof the split could drift to a codepoint boundary and
    the test would keep passing while testing nothing.
    """
    raw = fixture_bytes("fragmented_utf8")
    offset = raw.find(b"\xf0\x9d\x84\x9e")
    assert offset != -1

    for inside in (offset + 1, offset + 2, offset + 3):
        with pytest.raises(UnicodeDecodeError):
            raw[:inside].decode("utf-8")

        upstream.script("fragmented_utf8")
        upstream.set_rechunk([inside])
        _, _, body = post(upstream)
        assert body == raw


def test_rechunk_none_restores_frame_framing(upstream: MockUpstream) -> None:
    upstream.script("text_stream")
    upstream.set_rechunk([1])
    upstream.set_rechunk(None)
    _, _, body = post(upstream)
    assert body == fixture_bytes("text_stream")


def test_rechunk_rejects_non_positive_sizes(upstream: MockUpstream) -> None:
    """A zero-length chunk IS the HTTP terminator; accepting one would truncate.

    Rejected at the setter so a fuzzer that generates a 0 fails loudly instead of
    silently measuring a bug in this mock.
    """
    with pytest.raises(ValueError):
        upstream.set_rechunk([4, 0, 4])
    with pytest.raises(ValueError):
        upstream.set_rechunk([-1])


# --------------------------------------------------------------------------
# first-byte delay: the TTFT gate's realism knob
# --------------------------------------------------------------------------


def test_set_first_byte_delay_actually_delays(upstream: MockUpstream) -> None:
    """Plan 02-08 measures gateway overhead against this, not against localhost."""
    upstream.script("text_stream")

    start = time.perf_counter()
    post(upstream)
    baseline = time.perf_counter() - start
    assert baseline < 0.25, f"undelayed replay already took {baseline:.3f}s"

    upstream.set_first_byte_delay(0.3)
    start = time.perf_counter()
    _, _, body = post(upstream)
    delayed = time.perf_counter() - start

    assert delayed >= 0.25, f"expected >= 0.25s, measured {delayed:.3f}s"
    assert body == fixture_bytes("text_stream"), "delay must not alter the payload"


def test_first_byte_delay_rejects_negative(upstream: MockUpstream) -> None:
    with pytest.raises(ValueError):
        upstream.set_first_byte_delay(-0.1)


# --------------------------------------------------------------------------
# request capture and isolation
# --------------------------------------------------------------------------


def test_requests_capture_method_path_and_parsed_body(upstream: MockUpstream) -> None:
    """``_hermes_auto`` visibility is what plan 02-04 asserts stripping against."""
    upstream.script("text_stream")
    payload = {
        "model": "auto:balanced",
        "messages": [{"role": "user", "content": "synthetic"}],
        "_hermes_auto": {"lane": "main"},
    }
    post(upstream, payload, path="/v1/chat/completions")

    assert len(upstream.requests) == 1
    entry = upstream.requests[0]
    assert entry["method"] == "POST"
    assert entry["path"] == "/v1/chat/completions"
    assert entry["json"] == payload
    assert json.loads(entry["body"].decode("utf-8")) == payload
    assert entry["headers"]["Content-Type"] == "application/json"


def test_requests_returns_a_snapshot_not_the_live_list(upstream: MockUpstream) -> None:
    """A caller mutating the returned list must not corrupt the recorder."""
    upstream.script("text_stream")
    post(upstream)
    snapshot = upstream.requests
    snapshot.clear()
    assert len(upstream.requests) == 1


def test_non_json_body_is_recorded_without_raising(upstream: MockUpstream) -> None:
    """The oracle records what it receives; it never gatekeeps."""
    upstream.script("text_stream")
    conn = http.client.HTTPConnection("127.0.0.1", upstream.port, timeout=10)
    try:
        conn.request("POST", "/v1/chat/completions", body=b"\xff\xfe not json")
        assert conn.getresponse().read() == fixture_bytes("text_stream")
    finally:
        conn.close()
    assert upstream.requests[0]["json"] is None
    assert upstream.requests[0]["body"] == b"\xff\xfe not json"


def test_unscripted_request_fails_loudly(upstream: MockUpstream) -> None:
    """Better a named 500 than a hang or a replay of whatever ran last."""
    status, _, body = post(upstream)
    assert status == 500
    assert json.loads(body)["error"]["type"] == "mock_upstream_error"


def test_unknown_fixture_is_rejected_eagerly(upstream: MockUpstream) -> None:
    """Raised from ``script()``, not from a server thread as an opaque 500."""
    with pytest.raises(UnknownFixtureError):
        upstream.script("no_such_fixture")


def test_two_instances_bind_different_ports_and_do_not_interfere() -> None:
    """Parallel tests must never collide, which is why the mock binds port 0."""
    with MockUpstream() as first, MockUpstream() as second:
        assert first.port != second.port

        first.script("text_stream")
        second.script("refusal")

        _, _, first_body = post(first)
        _, _, second_body = post(second)

        assert first_body == fixture_bytes("text_stream")
        assert second_body == fixture_bytes("refusal")
        assert len(first.requests) == 1
        assert len(second.requests) == 1


def test_close_is_idempotent() -> None:
    server = MockUpstream()
    server.close()
    server.close()
