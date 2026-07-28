"""R5, proved fixture by fixture against a real supervised sidecar.

Three groups of tests live here, and the middle one is the reason the other two
are trustworthy.

1. **The corpus.** All twelve fixtures, each fetched direct from the mock and
   through the gateway, compared under both reductions plus status, headers, and
   transport-failure outcome. Then the individual properties the reductions
   alone would not name: the usage-only final chunk, the six-way argument
   fragmentation, the split UTF-8 codepoint, the CRLF keepalive stream, and the
   truncated upstream.

2. **The harness's own falsifiability.** A differential test that has never been
   shown to go red proves nothing. ``test_the_comparison_detects_*`` feeds the
   reductions deliberately wrong frame sequences and requires each to be caught
   -- and, just as importantly, requires transport re-chunking *not* to be
   caught, because a byte reduction that fired on a redrawn TCP boundary would
   be unusable.

3. **The token-scope boundary across the real two-listener deployment.** Plan
   02-06 asserted this inside its own app; here both listeners are actually
   running, which is the configuration a user gets.

**No production code may be edited to make anything here pass.** A failure is a
finding about the relay, and the plan says so explicitly: that inversion is how
a phase ships a green suite over a broken gateway.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from collections.abc import Iterator
from typing import Any

import httpx
import jsonschema
import pytest

from hermes_auto.gateway.errors import assert_error_body
from tests.differential import harness
from tests.differential.harness import (
    NON_STREAMING_FIXTURES,
    TRUNCATED_FIXTURES,
    Deployment,
    assert_identical,
    byte_reduction,
    describe_difference,
    run_direct,
    run_through_gateway,
    semantic_reduction,
)
from tests.integration.mock_upstream import FIXTURES, fixture_bytes, split_frames

pytestmark = pytest.mark.differential

FIXTURE_NAMES = sorted(FIXTURES)


@pytest.fixture(scope="module")
def deployed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    """One mock upstream and one real supervised sidecar for the whole module.

    Module-scoped because ``supervisor.start()`` spawns a detached process and
    waits for ``/healthz``; paying that per test would add roughly a second to
    each of thirty of them for no additional coverage. ``HERMES_AUTO_STATE_DIR``
    outranks configuration, so only one deployment can be live per process
    anyway.
    """
    root = pathlib.Path(tmp_path_factory.mktemp("differential"))
    with harness.deployment(root) as deployment:
        yield deployment


# ---------------------------------------------------------------------------
# 1. The corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_fixture_is_identical_under_both_reductions(
    fixture_name: str, deployed: Deployment
) -> None:
    """The phase's central claim, once per fixture.

    ``assert_identical`` compares the byte reduction (catches a dropped,
    reordered, merged, or truncated frame), the semantic reduction (catches
    corrupted reassembly), the status, the transport-failure outcome, and every
    non-hop-by-hop response header.
    """
    direct, relayed = assert_identical(fixture_name, deployed)

    # The mock replays a fixed file, so the stronger raw claim also holds: there
    # is no clock or generated id anywhere in the corpus for the byte
    # reduction's two rewrites to be hiding.
    assert relayed.body == direct.body == fixture_bytes(fixture_name)


def test_every_fixture_in_the_corpus_is_covered() -> None:
    """A parametrized list that silently shrank would still be green."""
    assert len(FIXTURE_NAMES) == 12
    assert set(FIXTURE_NAMES) == set(FIXTURES)


def test_usage_only_final_chunk_survives_the_gateway(deployed: Deployment) -> None:
    """The single most likely silent loss in the whole relay.

    A final chunk with ``choices: []`` and a populated ``usage`` carries the
    token accounting and nothing else. A relay that models "a chunk has choices"
    drops it, every assembled-JSON comparison still passes, and the caller
    silently loses its cost telemetry. So it is asserted structurally here and
    not only through the reductions.
    """
    direct, relayed = assert_identical("usage_only_final_chunk", deployed)

    reduced = semantic_reduction(relayed.frames)
    assert reduced["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 4,
        "total_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
        "completion_tokens_details": {
            "reasoning_tokens": 0,
            "audio_tokens": 0,
            "accepted_prediction_tokens": 0,
            "rejected_prediction_tokens": 0,
        },
    }
    assert reduced == semantic_reduction(direct.frames)

    # And the frame itself is on the wire, with its empty choices list intact.
    usage_frames = [
        frame
        for frame in split_frames(relayed.body)
        if b'"choices":[]' in frame and b'"usage"' in frame
    ]
    assert len(usage_frames) == 1, (
        "the usage-only final chunk did not survive the relay: "
        f"{len(usage_frames)} such frames in {len(split_frames(relayed.body))}"
    )
    assert reduced["done_sentinel"] is True


def test_fragmented_arguments_are_neither_corrupted_nor_reassembled(
    deployed: Deployment,
) -> None:
    """Both halves of the claim, because either alone is satisfiable by a bug.

    The semantic reduction proves the six fragments concatenate into parseable
    JSON. The byte reduction proves the gateway did not *help* by joining them:
    a relay that buffered until it could emit one well-formed tool call would
    pass the semantic check and change what the client sees.
    """
    direct, relayed = assert_identical("fragmented_arguments", deployed)

    call = semantic_reduction(relayed.frames)["choices"]["0"]["tool_calls"]["0"]
    assert call["arguments_parse_ok"] is True
    assert call["arguments_json"] == {
        "path": "/tmp/synthetic.txt",
        "arguments": ["--dry-run"],
        "mode": "read",
    }
    assert call["name"] == "run_task"
    assert call["id"] == "call_test0004"

    # Not reassembled: the fragments are still six separate frames, and the
    # `"argu` / `ments"` split is still on the wire.
    # The opening delta also carries `"arguments":""`; the six *fragments* are
    # the frames whose argument string is non-empty.
    fragment_frames = [
        frame
        for frame in split_frames(relayed.body)
        if b'"arguments":"' in frame and b'"arguments":""' not in frame
    ]
    assert len(fragment_frames) == 6, (
        f"expected 6 argument fragments on the wire, saw {len(fragment_frames)}"
    )
    assert b'\\"argu' in relayed.body and b'ments\\"' in relayed.body
    assert relayed.body == direct.body

    # No individual fragment is valid JSON on its own -- which is what makes the
    # concatenation load-bearing rather than incidental.
    fragments = [
        json.loads(frame[len(b"data: ") :].strip())["choices"][0]["delta"][
            "tool_calls"
        ][0]["function"]["arguments"]
        for frame in fragment_frames
    ]
    assert all(_is_not_json(part) for part in fragments if part)


def _is_not_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return True
    return False


def test_parallel_tool_calls_assemble_on_index_not_on_name(
    deployed: Deployment,
) -> None:
    """Two calls, same function name, interleaved. ``index`` is the only key."""
    _, relayed = assert_identical("parallel_tool_calls", deployed)
    calls = semantic_reduction(relayed.frames)["choices"]["0"]["tool_calls"]

    assert set(calls) == {"0", "1"}
    assert calls["0"]["name"] == calls["1"]["name"] == "read_file"
    assert calls["0"]["id"] != calls["1"]["id"]
    for call in calls.values():
        assert call["arguments_parse_ok"] is True
        assert isinstance(call["arguments_json"], dict)
    # Assembled onto the right call: keying on name would merge them.
    assert calls["0"]["arguments_json"] != calls["1"]["arguments_json"]


def test_fragmented_utf8_content_is_not_mangled(deployed: Deployment) -> None:
    """A relay that decodes per chunk turns this into replacement characters.

    Two hazards in one fixture: raw 2-, 3- and 4-byte UTF-8 sequences in the
    payload, and a U+1F600 delivered as two lone surrogate halves in adjacent
    frames. Neither half is encodable alone, so a relay that re-encodes each
    delta raises; a relay that decodes the transport stream with ``errors=
    "replace"`` substitutes U+FFFD.
    """
    direct, relayed = assert_identical("fragmented_utf8", deployed)

    relayed_content = semantic_reduction(relayed.frames)["choices"]["0"]["content"]
    direct_content = semantic_reduction(direct.frames)["choices"]["0"]["content"]
    assert relayed_content == direct_content
    assert "�" not in relayed_content, "a replacement character reached the client"

    assert "café crème" in relayed_content
    assert "日本" in relayed_content
    assert "\U0001d11e" in relayed_content
    # The surrogate halves survive as halves. Joining them would be a repair the
    # provider did not perform, and it changes the bytes the caller sees.
    assert "\ud83d" in relayed_content and "\ude00" in relayed_content

    # The raw multi-byte sequences are on the wire undecoded.
    for sequence in (b"\xc3\xa9", b"\xe6\x97\xa5", b"\xf0\x9d\x84\x9e"):
        assert sequence in relayed.body


def test_empty_deltas_and_crlf_keepalives_both_survive(
    deployed: Deployment,
) -> None:
    """The one CRLF fixture. A relay hardcoding LF breaks only on this one."""
    direct, relayed = assert_identical("empty_deltas_and_keepalives", deployed)

    assert b"\r\n\r\n" in relayed.body
    assert b"\n\n" not in relayed.body.replace(b"\r\n\r\n", b""), (
        "a CRLF terminator was rewritten to LF"
    )
    assert relayed.body.count(b": ping") == 2, "an SSE comment keepalive was swallowed"
    assert relayed.body.count(b'"delta":{}') == 3, "an empty delta was dropped"
    assert b'"choices":[]' in relayed.body
    assert relayed.body == direct.body

    # LF fixtures are untouched in the other direction, on the same gateway.
    _, lf = assert_identical("text_stream", deployed)
    assert b"\r\n" not in lf.body


def test_text_stream_relays_an_unmodelled_provider_field(
    deployed: Deployment,
) -> None:
    """``reasoning_content`` is in no schema this project owns. It must survive."""
    _, relayed = assert_identical("text_stream", deployed)
    assert b'"reasoning_content"' in relayed.body
    assert semantic_reduction(relayed.frames)["choices"]["0"]["reasoning_content"]


def test_refusal_relays_its_finish_reason(deployed: Deployment) -> None:
    _, relayed = assert_identical("refusal", deployed)
    reduced = semantic_reduction(relayed.frames)
    assert reduced["choices"]["0"]["finish_reason"] == "content_filter"
    assert reduced["choices"]["0"]["refusal"]


def test_a_truncated_upstream_fails_identically_through_both_paths(
    deployed: Deployment,
) -> None:
    """The case most likely to expose a divergence.

    A gateway that converts a truncated upstream stream into a clean 200 has
    told the client the generation completed when it did not. Both runs must
    fail, with the same exception class, having delivered the same bytes first.
    """
    direct, relayed = assert_identical("midstream_disconnect", deployed)

    assert direct.failure is not None, "the fixture is supposed to truncate"
    assert relayed.failure == direct.failure
    assert relayed.status == direct.status == 200

    # The bytes that did arrive are complete and identical, and the stream is
    # genuinely unterminated.
    assert relayed.body == direct.body
    assert b"[DONE]" not in relayed.body
    reduced = semantic_reduction(relayed.frames)
    assert reduced["done_sentinel"] is False
    assert reduced["unparsed"], "the truncated tail was silently dropped"


@pytest.mark.parametrize("fixture_name", sorted(NON_STREAMING_FIXTURES))
def test_error_fixtures_relay_status_body_and_headers_with_no_sse_frame(
    fixture_name: str, deployed: Deployment
) -> None:
    """Errors come back as the provider wrote them, not reinterpreted.

    A gateway that turned a 429 into its own error envelope would break every
    retry policy keyed on ``Retry-After``, and one that emitted an SSE frame for
    a non-streaming error would break the SDK's content-type dispatch.
    """
    direct, relayed = assert_identical(fixture_name, deployed)

    expected = fixture_bytes(fixture_name)
    assert relayed.body == direct.body == expected
    assert relayed.status == FIXTURES[fixture_name].status
    assert b"data:" not in relayed.body, "a spurious SSE frame was emitted"
    assert relayed.failure is None

    headers = dict(relayed.headers)
    assert headers["content-type"] == "application/json"
    assert headers["content-length"] == str(len(expected))
    if fixture_name == "error_429":
        assert headers["retry-after"] == "20", (
            "dropping Retry-After turns a recoverable throttle into an opaque "
            "failure for the caller"
        )

    # The body is the provider's own envelope, unchanged.
    assert_error_body(json.loads(relayed.body))


def test_hermes_auto_never_reaches_the_upstream(deployed: Deployment) -> None:
    """The private plugin-to-gateway channel is stripped before the hop."""
    deployed.upstream.clear_requests()
    run_through_gateway("text_stream", deployed)

    received = deployed.upstream.requests
    assert received, "the gateway never reached the upstream"
    for entry in received:
        assert b"_hermes_auto" not in entry["body"]
        assert isinstance(entry["json"], dict)
        assert "_hermes_auto" not in entry["json"]
        assert entry["json"]["model"] == "auto:balanced"
    deployed.upstream.clear_requests()


def test_the_gateway_run_really_is_a_separate_supervised_process(
    deployed: Deployment,
) -> None:
    """Guards the assertion of record against a silent downgrade.

    If this module were ever refactored onto ``TestClient`` or an in-process
    ASGI transport, every framing test above would keep passing while testing
    nothing: both of those buffer the whole response before returning it. The
    proof that it has not happened is a live ``/healthz`` on a port the runtime
    file recorded, answering with this deployment's own instance id.
    """
    from hermes_auto import supervisor
    from hermes_auto.state.runtime import read_runtime

    record = read_runtime(None)
    assert record is not None and record.port == deployed.port
    assert record.pid != os.getpid(), (
        "the gateway is running inside the test process, so uvicorn's framing "
        "layer -- the layer this suite exists to test -- is not being exercised"
    )

    response = httpx.get(f"{deployed.gateway_url}/healthz", timeout=10)
    assert response.status_code == 200
    assert response.json()["instance_id"] == deployed.instance_id
    assert supervisor.status().running


# ---------------------------------------------------------------------------
# 2. Proving the comparison can go red
# ---------------------------------------------------------------------------


def _frames(name: str) -> list[bytes]:
    return split_frames(fixture_bytes(name))


def _reassembled_tool_call() -> tuple[list[bytes], list[bytes]]:
    """``fragmented_arguments``, and the same call emitted as a single frame.

    This is what a well-meaning relay that buffered until it could emit one
    complete tool call would produce. Semantically identical, byte-wise not.
    """
    original = _frames("fragmented_arguments")
    fragments = original[2:8]

    def arguments_of(frame: bytes) -> str:
        event = json.loads(frame[len(b"data: ") :].strip())
        return event["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]

    event = json.loads(fragments[0][len(b"data: ") :].strip())
    event["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = "".join(
        arguments_of(frame) for frame in fragments
    )
    merged = b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"
    return original, original[:2] + [merged] + original[8:]


CORRUPTIONS: list[tuple[str, str]] = [
    ("dropped_usage_chunk", "usage_only_final_chunk"),
    ("dropped_done_sentinel", "usage_only_final_chunk"),
    ("reordered_frames", "usage_only_final_chunk"),
    ("flipped_byte", "usage_only_final_chunk"),
    ("merged_frame_terminator", "usage_only_final_chunk"),
    ("changed_model", "text_stream"),
    ("dropped_reasoning_content", "text_stream"),
    ("changed_tool_call_id", "fragmented_arguments"),
    ("lost_argument_fragment", "fragmented_arguments"),
    ("corrupted_argument_fragment", "fragmented_arguments"),
    ("decoded_per_chunk_mid_codepoint", "fragmented_utf8"),
    ("joined_surrogate_halves", "fragmented_utf8"),
    ("swallowed_keepalives", "empty_deltas_and_keepalives"),
    ("crlf_rewritten_to_lf", "empty_deltas_and_keepalives"),
    ("dropped_truncated_tail", "midstream_disconnect"),
    ("reassembled_tool_call", "fragmented_arguments"),
]


def _corrupt(kind: str, name: str) -> tuple[list[bytes], list[bytes]]:
    original = _frames(name)
    if kind == "dropped_usage_chunk":
        return original, original[:3] + original[4:]
    if kind == "dropped_done_sentinel":
        return original, original[:-1]
    if kind == "reordered_frames":
        return original, [original[1], original[0], *original[2:]]
    if kind == "flipped_byte":
        head = original[0]
        return original, [head[:120] + bytes([head[120] ^ 1]) + head[121:], *original[1:]]
    if kind == "merged_frame_terminator":
        return original, [original[0].rstrip(b"\r\n") + original[1], *original[2:]]
    if kind == "changed_model":
        return original, [f.replace(b"test-model-a", b"test-model-b") for f in original]
    if kind == "dropped_reasoning_content":
        return original, [f for f in original if b"reasoning_content" not in f]
    if kind == "changed_tool_call_id":
        return original, [f.replace(b"call_test0004", b"call_zzzz9999") for f in original]
    if kind == "lost_argument_fragment":
        return original, original[:5] + original[6:]
    if kind == "corrupted_argument_fragment":
        mutated = list(original)
        mutated[3] = mutated[3].replace(b"th", b"tx")
        return original, mutated
    if kind == "decoded_per_chunk_mid_codepoint":
        body = b"".join(original)
        return original, [
            body[:1098].decode("utf-8", "replace").encode("utf-8"),
            body[1098:].decode("utf-8", "replace").encode("utf-8"),
        ]
    if kind == "joined_surrogate_halves":
        return original, [
            f.replace(b"\\ud83d", b"").replace(b"\\ude00", b"\\ud83d\\ude00")
            for f in original
        ]
    if kind == "swallowed_keepalives":
        return original, [f for f in original if not f.startswith(b":")]
    if kind == "crlf_rewritten_to_lf":
        return original, [f.replace(b"\r\n", b"\n") for f in original]
    if kind == "dropped_truncated_tail":
        return original, original[:-1]
    if kind == "reassembled_tool_call":
        return _reassembled_tool_call()
    raise AssertionError(f"unknown corruption {kind!r}")


@pytest.mark.parametrize(("kind", "fixture_name"), CORRUPTIONS, ids=[c[0] for c in CORRUPTIONS])
def test_the_byte_reduction_detects(kind: str, fixture_name: str) -> None:
    """Every corruption above must turn the byte comparison red.

    A green differential suite that has never been shown to go red proves
    nothing. This is the proof.
    """
    original, mutated = _corrupt(kind, fixture_name)
    assert byte_reduction(original) != byte_reduction(mutated), (
        f"{kind} on {fixture_name} passed the byte reduction unnoticed"
    )


#: Corruptions the *semantic* reduction is deliberately blind to, because each
#: is a reframing rather than a change of meaning. Listing them explicitly is
#: what keeps that blindness a decision rather than an accident: if one of these
#: ever starts being caught, the semantic reduction has grown frame-sensitive
#: and stopped being able to explain a byte difference.
SEMANTICALLY_INVISIBLE = {
    "reordered_frames",
    "swallowed_keepalives",
    "crlf_rewritten_to_lf",
    "reassembled_tool_call",
}


@pytest.mark.parametrize(("kind", "fixture_name"), CORRUPTIONS, ids=[c[0] for c in CORRUPTIONS])
def test_the_semantic_reduction_separates_corruption_from_reframing(
    kind: str, fixture_name: str
) -> None:
    original, mutated = _corrupt(kind, fixture_name)
    same = semantic_reduction(original) == semantic_reduction(mutated)
    if kind in SEMANTICALLY_INVISIBLE:
        assert same, (
            f"{kind} changed the semantic reduction. It is a pure reframing, so "
            f"the two reductions no longer say different things and the harness "
            f"has lost its ability to diagnose a byte difference."
        )
    else:
        assert not same, f"{kind} passed the semantic reduction unnoticed"


def test_transport_rechunking_is_invisible_to_both_reductions() -> None:
    """The one difference the harness must NOT report.

    TCP and the ASGI server redraw chunk boundaries freely. A reduction that
    fired on that would be red on every run and would be deleted within a week,
    taking the real coverage with it. The concatenation is the comparable thing.
    """
    original = _frames("fragmented_utf8")
    body = b"".join(original)
    for label, alternative in (
        ("one chunk", [body]),
        ("one byte per chunk", [bytes([byte]) for byte in body]),
        ("seven-byte chunks", [body[i : i + 7] for i in range(0, len(body), 7)]),
    ):
        assert byte_reduction(original) == byte_reduction(alternative), label
        assert semantic_reduction(original) == semantic_reduction(alternative), label


def test_only_id_and_created_are_rewritten() -> None:
    """The two permitted fields, and the proof that it is only those two."""
    original = _frames("fragmented_arguments")

    varied_id = [f.replace(b"chatcmpl-test0004", b"chatcmpl-live99999") for f in original]
    varied_created = [f.replace(b"1700000000", b"1799999999") for f in original]
    assert byte_reduction(original) == byte_reduction(varied_id)
    assert byte_reduction(original) == byte_reduction(varied_created)

    # Nothing else is forgiven -- including the *nested* id of a tool call,
    # which shares the key name with the field that is.
    for label, mutated in (
        ("tool-call id", [f.replace(b"call_test0004", b"call_x") for f in original]),
        ("model", [f.replace(b"test-model-a", b"test-model-b") for f in original]),
        ("object", [f.replace(b"chat.completion.chunk", b"chat.completion") for f in original]),
        ("index", [f.replace(b'"index":0', b'"index":1') for f in original]),
    ):
        assert byte_reduction(original) != byte_reduction(mutated), label

    # And the rewrite is anchored: a nested "created" is not touched either.
    nested = [f.replace(b'"finish_reason":null', b'"meta":{"created":42}') for f in original]
    assert b'"created":42' in byte_reduction(nested)


def test_the_failure_message_names_a_frame_index_and_a_byte_offset() -> None:
    """A message that says only "bytes differ" costs an hour of bisection."""
    original = _frames("usage_only_final_chunk")
    mutated = original[:3] + original[4:]
    message = describe_difference(b"".join(original), b"".join(mutated))

    assert "first difference at byte offset" in message
    assert re.search(r"frame index \d+", message), message
    assert "frame-relative byte" in message
    assert "direct  frame:" in message and "relayed frame:" in message


def test_assert_identical_reports_a_divergence_rather_than_swallowing_it(
    deployed: Deployment,
) -> None:
    """The end-to-end proof that the assertion path itself can fail.

    A gateway run is compared against a *deliberately mismatched* direct run --
    a different fixture -- so the comparison has something real to catch. If
    this passes silently, ``assert_identical`` is not asserting.
    """
    direct = run_direct("text_stream", deployed)
    relayed = run_through_gateway("usage_only_final_chunk", deployed)

    assert byte_reduction(direct.frames) != byte_reduction(relayed.frames)
    assert semantic_reduction(direct.frames) != semantic_reduction(relayed.frames)

    with pytest.raises(AssertionError, match="byte reduction differs"):
        _compare(direct, relayed, "cross-fixture control")


def _compare(direct: Any, relayed: Any, label: str) -> None:
    """The core of ``assert_identical``, applied to two results already in hand."""
    reduced_direct = byte_reduction(direct.frames)
    reduced_relayed = byte_reduction(relayed.frames)
    assert reduced_relayed == reduced_direct, (
        f"{label}: byte reduction differs.\n"
        + describe_difference(reduced_direct, reduced_relayed)
    )


# ---------------------------------------------------------------------------
# 3. Token scope, across both live listeners
# ---------------------------------------------------------------------------


ADMIN_PATHS = [
    ("GET", "/admin/v1/status"),
    ("GET", "/admin/v1/decisions/d-1"),
    ("POST", "/admin/v1/sessions/s-1/reroute"),
    ("POST", "/admin/v1/sessions/s-1/pin"),
    ("POST", "/admin/v1/feedback"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_PATHS)
def test_the_inference_token_is_rejected_by_every_admin_endpoint(
    method: str, path: str, deployed: Deployment
) -> None:
    """Two secrets, two scopes, two listeners -- proved with both running.

    Plan 02-06 asserted this within its own app. Here the admin listener is a
    second socket in a supervised process, which is the configuration a user
    actually gets, and the one where a wiring mistake in ``main.py`` would show.
    """
    response = httpx.request(
        method,
        f"{deployed.admin_url}{path}",
        headers={"Authorization": f"Bearer {deployed.token}"},
        timeout=15,
    )
    assert response.status_code == 401, (
        f"{method} {path} accepted the INFERENCE token: {response.status_code}"
    )

    # The same request with the admin token is not a 401, which is what makes
    # the assertion above about scope rather than about the endpoint being dead.
    allowed = httpx.request(
        method,
        f"{deployed.admin_url}{path}",
        headers={"Authorization": f"Bearer {deployed.admin_token}"},
        timeout=15,
    )
    assert allowed.status_code != 401
    assert allowed.status_code in (200, 501)


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/v1/chat/completions"), ("GET", "/v1/models")],
)
def test_the_admin_token_is_rejected_by_the_inference_listener(
    method: str, path: str, deployed: Deployment
) -> None:
    deployed.upstream.script("text_stream")
    response = httpx.request(
        method,
        f"{deployed.gateway_url}{path}",
        headers={"Authorization": f"Bearer {deployed.admin_token}"},
        json=harness.chat_body() if method == "POST" else None,
        timeout=15,
    )
    assert response.status_code == 401, (
        f"{method} {path} accepted the ADMIN token: {response.status_code}"
    )
    body = response.json()
    assert_error_body(body)
    assert body["error"]["code"] == "invalid_api_key"

    allowed = httpx.request(
        method,
        f"{deployed.gateway_url}{path}",
        headers={"Authorization": f"Bearer {deployed.token}"},
        json=harness.chat_body() if method == "POST" else None,
        timeout=30,
    )
    assert allowed.status_code == 200


def test_the_two_tokens_are_different_secrets_in_different_files(
    deployed: Deployment,
) -> None:
    assert deployed.token != deployed.admin_token
    assert (deployed.state_dir / "token").read_text(encoding="utf-8").strip() == (
        deployed.token
    )
    assert (deployed.state_dir / "admin-token").read_text(
        encoding="utf-8"
    ).strip() == deployed.admin_token


def test_the_admin_surface_is_absent_from_the_inference_listener(
    deployed: Deployment,
) -> None:
    """Distinct surfaces, not one app answering on two ports."""
    response = httpx.get(
        f"{deployed.gateway_url}/admin/v1/status",
        headers={"Authorization": f"Bearer {deployed.admin_token}"},
        timeout=15,
    )
    assert response.status_code == 404
    # And the inference surface is absent from the admin listener: unknown paths
    # there are 401, because auth is middleware rather than per-handler.
    assert (
        httpx.get(f"{deployed.admin_url}/healthz", timeout=15).status_code == 401
    )


# ---------------------------------------------------------------------------
# The corpus finding this plan owns: `error.code` as an integer
# ---------------------------------------------------------------------------


def test_the_corpus_contains_no_integer_error_code() -> None:
    """Records a gap rather than asserting one away.

    Plans 02-02 and 02-04 both flagged that some OpenAI-compatible relays emit an
    **integer** ``error.code`` -- an HTTP status -- where ``openai-error.v1``
    types it ``["string", "null"]``. This plan owns confirming it in the corpus.
    It is not there: all three error fixtures carry a string code. So the risk is
    real and currently unrepresented, and no test in this repository exercises
    it. That is the finding.
    """
    codes = {
        name: json.loads(fixture_bytes(name))["error"]["code"]
        for name in sorted(NON_STREAMING_FIXTURES)
    }
    assert codes == {
        "error_401": "invalid_api_key",
        "error_429": "rate_limit_exceeded",
        "error_context_length": "context_length_exceeded",
    }
    assert all(isinstance(code, str) for code in codes.values())


def test_an_integer_error_code_is_accepted_and_relayed_verbatim() -> None:
    """The closure this plan reported has been made; the tripwire it left is spent.

    ``openai-error.v1``'s own description calls itself a RELAY schema and says a
    closed object "would reject traffic the gateway is required to pass
    through". ``code: ["string", "null"]`` was exactly such a closure applied to
    a named field: some OpenAI-compatible relays put an HTTP status there, and
    such a body is one the gateway relays correctly and byte-for-byte while
    ``assert_error_body`` rejected it. So the failure was in a contract test
    rather than in the gateway, and the fix was to widen the schema to
    ``["string", "integer", "null"]`` rather than to weaken the assertion --
    which is the only thing checking the two fields that *are* required.

    The predecessor of this test asserted the *rejection*, deliberately, as a
    tripwire that would fail the moment the schema was widened. It has been
    deleted rather than edited into agreement with the new behavior, because a
    tripwire quietly retargeted at its own fix proves nothing. What survives
    unchanged is its relay half: the gateway never parses the body, so an
    integer code must still reach the caller exactly as the provider wrote it.
    """
    integer_code = {
        "error": {
            "message": "Rate limit reached",
            "type": "requests",
            "param": None,
            "code": 429,
        }
    }

    # The widened schema accepts it. This is the assertion that replaces the
    # tripwire, and it fails against the pre-widening schema.
    assert_error_body(integer_code)

    # The neighbouring shapes still pass, so the widening did not dissolve the
    # field's type constraint into "anything".
    assert_error_body({"error": {"message": "m", "type": "t", "code": "429"}})
    assert_error_body({"error": {"message": "m", "type": "t"}})
    assert_error_body({"error": {"message": "m", "type": "t", "code": None}})

    # A code that is neither string, integer nor null is still a violation:
    # widening by one type is not the same as removing the constraint.
    with pytest.raises(jsonschema.ValidationError):
        assert_error_body({"error": {"message": "m", "type": "t", "code": [429]}})

    # And the relay itself is indifferent: the gateway never parses the body,
    # so an integer code reaches the caller exactly as the provider wrote it.
    raw = json.dumps(integer_code, separators=(",", ":")).encode()
    assert byte_reduction([raw]) == raw
    assert semantic_reduction([raw])["json"] == integer_code


def test_truncated_fixture_set_is_what_the_corpus_says() -> None:
    """A guard on the harness's own fixture classification."""
    assert TRUNCATED_FIXTURES == {"midstream_disconnect"}
    assert NON_STREAMING_FIXTURES == {
        "error_401",
        "error_429",
        "error_context_length",
    }
    for name in FIXTURE_NAMES:
        assert harness.is_streaming_fixture(name) is (
            name not in NON_STREAMING_FIXTURES
        )
