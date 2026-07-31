"""The chunk-boundary fuzzer: the same bytes, re-split anywhere, must relay the same.

SSE relays do not break on well-formed frames. They break on frames split across
TCP reads -- a ``data:`` prefix arriving in two pieces, a frame terminator
straddling a read, a multi-byte UTF-8 codepoint delivered half now and half
later. A relay that buffers to a line, decodes to text, or waits for a terminator
looks perfect against a fixture replayed one frame per chunk and fails the moment
the upstream chunks differently. That is why this is the highest-yield test in
the phase.

``MockUpstream.set_rechunk(sizes)`` re-splits the *identical* response bytes at
arbitrary offsets, changing framing and never content. ``hypothesis`` draws the
offsets. The assertion is that both reductions of what the client receives are
invariant to the re-split -- compared not against another run but against the
fixture's own on-disk bytes, so a re-split that corrupted both paths together
would still be caught.

**Excluded re-split classes, and why.** Two, both recorded rather than quietly
dropped from the strategy:

1. **A zero-length chunk.** ``set_rechunk`` rejects it, and correctly: a
   zero-length chunk *is* the HTTP/1.1 chunked terminator, so emitting one
   mid-body would end the response early. That would measure a defect in the
   mock, not in the relay. Offsets are therefore drawn strictly inside
   ``(0, len(body))`` and deduplicated.
2. **Nothing else.** In particular, splits landing inside the trailing
   ``data: [DONE]`` sentinel are *not* excluded -- the plan permitted excluding
   them if genuinely untestable, and they are not. They are drawn by the general
   strategy and pinned explicitly by
   ``test_a_split_inside_the_done_sentinel_is_relayed_intact``.

Runtime is recorded in the plan summary; ``MAX_EXAMPLES`` below is the knob.
"""

from __future__ import annotations

import pathlib
import time
from collections.abc import Iterator, Sequence

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.differential import harness
from tests.differential.harness import (
    Deployment,
    baseline_reductions,
    byte_reduction,
    describe_difference,
    run_direct,
    run_through_gateway,
    semantic_reduction,
)
from tests.integration.mock_upstream import MockUpstream, fixture_bytes, split_frames

pytestmark = pytest.mark.differential

#: Examples per fixture. Chosen to keep the whole module under about 60 seconds
#: on the development machine -- measured at roughly 12 s for 4 fixtures at this
#: setting, plus one sidecar start. Raising it costs one loopback round trip per
#: example.
MAX_EXAMPLES = 60

#: The three the plan requires, plus the CRLF fixture. Re-chunking a CRLF stream
#: is a distinct hazard: a relay that scanned for ``\n\n`` and happened to be
#: handed ``\r\n`` and ``\r\n`` in separate reads fails here and nowhere else.
FUZZED_FIXTURES = [
    "fragmented_arguments",
    "fragmented_utf8",
    "usage_only_final_chunk",
    "empty_deltas_and_keepalives",
]

_SETTINGS = settings(
    max_examples=MAX_EXAMPLES,
    # Each example is a real HTTP round trip to a real subprocess. Hypothesis's
    # default per-example deadline is about what one of those costs on a loaded
    # Windows box, and a deadline breach here would report as a flaky failure of
    # the property rather than as the timing fact it is.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


@pytest.fixture(scope="module")
def deployed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    """One mock upstream and one real supervised sidecar for the whole module."""
    root = pathlib.Path(tmp_path_factory.mktemp("fuzz"))
    with harness.deployment(root) as deployment:
        yield deployment


def sizes_from_offsets(offsets: Sequence[int], length: int) -> list[int]:
    """Turn cut positions into the chunk sizes ``set_rechunk`` takes.

    Offsets are deduplicated, sorted, and clamped to ``1 <= o < length``, which
    is the only exclusion this fuzzer applies -- see the module docstring.
    """
    cuts = sorted({o for o in offsets if 0 < o < length})
    sizes: list[int] = []
    previous = 0
    for cut in cuts:
        sizes.append(cut - previous)
        previous = cut
    sizes.append(length - previous)
    assert all(size > 0 for size in sizes)
    assert sum(sizes) == length
    return sizes


def assert_invariant(
    fixture_name: str, deployment: Deployment, sizes: Sequence[int] | None
) -> None:
    """The property: re-chunking changes framing, never what the client sees."""
    expected_bytes, expected_semantics = baseline_reductions(fixture_name)
    relayed = run_through_gateway(fixture_name, deployment, rechunk=sizes)

    assert relayed.status == 200
    assert relayed.failure is None, (
        f"{fixture_name} re-split into {len(sizes) if sizes else 1} chunks ended "
        f"in a transport error: {relayed.failure}"
    )

    reduced = byte_reduction(relayed.frames)
    assert reduced == expected_bytes, (
        f"{fixture_name}: upstream re-chunking changed the relayed bytes.\n"
        f"chunk sizes: {list(sizes)[:40] if sizes else 'default'}\n"
        + describe_difference(expected_bytes, reduced)
    )
    assert semantic_reduction(relayed.frames) == expected_semantics, (
        f"{fixture_name}: upstream re-chunking changed the assembled meaning "
        f"with chunk sizes {list(sizes)[:40] if sizes else 'default'}"
    )


# ---------------------------------------------------------------------------
# The fuzzer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", FUZZED_FIXTURES)
@given(data=st.data())
@_SETTINGS
def test_both_reductions_are_invariant_to_arbitrary_rechunking(
    fixture_name: str, deployed: Deployment, data: st.DataObject
) -> None:
    """Re-split the upstream bytes anywhere; the client must see the same stream."""
    length = len(fixture_bytes(fixture_name))
    offsets = data.draw(
        st.lists(st.integers(min_value=1, max_value=length - 1), max_size=24),
        label="cut offsets",
    )
    assert_invariant(fixture_name, deployed, sizes_from_offsets(offsets, length))


@pytest.mark.parametrize("fixture_name", FUZZED_FIXTURES)
@given(data=st.data())
@_SETTINGS
def test_uniform_rechunking_is_invariant(
    fixture_name: str, deployed: Deployment, data: st.DataObject
) -> None:
    """Fixed-width chunks, which is what a real transport actually produces.

    A random set of cut offsets clusters; a uniform width sweeps every phase of
    the frame structure in turn, which is how a split lands one byte before a
    terminator on one run and one byte after it on the next.
    """
    body = fixture_bytes(fixture_name)
    width = data.draw(st.integers(min_value=1, max_value=len(body)), label="width")
    sizes = [width] * ((len(body) + width - 1) // width)
    assert_invariant(fixture_name, deployed, sizes)


# ---------------------------------------------------------------------------
# The degenerate cases, pinned explicitly rather than left to the draw
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", FUZZED_FIXTURES)
def test_the_whole_body_in_one_chunk(
    fixture_name: str, deployed: Deployment
) -> None:
    assert_invariant(fixture_name, deployed, [len(fixture_bytes(fixture_name))])


@pytest.mark.parametrize("fixture_name", FUZZED_FIXTURES)
def test_one_byte_per_chunk(fixture_name: str, deployed: Deployment) -> None:
    """The most adversarial framing there is: every read boundary is a split."""
    assert_invariant(fixture_name, deployed, [1] * len(fixture_bytes(fixture_name)))


@pytest.mark.parametrize("fixture_name", FUZZED_FIXTURES)
def test_splits_exactly_on_frame_boundaries(
    fixture_name: str, deployed: Deployment
) -> None:
    """The mock's own default, stated as sizes so the path is the same one."""
    frames = split_frames(fixture_bytes(fixture_name))
    assert_invariant(fixture_name, deployed, [len(frame) for frame in frames])


@pytest.mark.parametrize("fixture_name", FUZZED_FIXTURES)
def test_splits_inside_the_data_prefix(
    fixture_name: str, deployed: Deployment
) -> None:
    """A cut between ``d`` and ``ata: `` on every frame.

    A relay that recognised the ``data: `` prefix -- to count events, to inject a
    keepalive, to do anything at all -- breaks here first.
    """
    body = fixture_bytes(fixture_name)
    offsets: list[int] = []
    start = 0
    while True:
        found = body.find(b"data: ", start)
        if found == -1:
            break
        offsets.extend([found + 1, found + 3, found + len(b"data: ")])
        start = found + 1
    assert offsets, "fixture has no data: prefix to split inside"
    assert_invariant(fixture_name, deployed, sizes_from_offsets(offsets, len(body)))


@pytest.mark.parametrize("fixture_name", FUZZED_FIXTURES)
def test_splits_inside_every_frame_terminator(
    fixture_name: str, deployed: Deployment
) -> None:
    """A cut between the two newlines -- and, for CRLF, inside each pair."""
    body = fixture_bytes(fixture_name)
    terminator = b"\r\n\r\n" if b"\r\n\r\n" in body else b"\n\n"
    offsets: list[int] = []
    start = 0
    while True:
        found = body.find(terminator, start)
        if found == -1:
            break
        offsets.extend(found + step for step in range(1, len(terminator)))
        start = found + len(terminator)
    assert offsets, "fixture has no frame terminator to split inside"
    assert_invariant(fixture_name, deployed, sizes_from_offsets(offsets, len(body)))


def test_a_split_inside_the_done_sentinel_is_relayed_intact(
    deployed: Deployment,
) -> None:
    """The class the plan allowed excluding. It is testable, so it is not excluded.

    Every byte position inside the trailing ``data: [DONE]`` is used as a cut, in
    turn. A relay that special-cased the sentinel -- to know when to close, to
    stop reading, to append its own -- fails here.
    """
    for fixture_name in ("usage_only_final_chunk", "fragmented_arguments"):
        body = fixture_bytes(fixture_name)
        sentinel = body.rindex(b"data: [DONE]")
        for cut in range(sentinel, sentinel + len(b"data: [DONE]")):
            assert_invariant(
                fixture_name, deployed, sizes_from_offsets([cut], len(body))
            )


def test_a_split_inside_a_multibyte_codepoint_is_relayed_intact(
    deployed: Deployment,
) -> None:
    """The split that separates a byte-transparent relay from a decoding one.

    Plan 02-03 placed raw 2-, 3- and 4-byte UTF-8 sequences in
    ``fragmented_utf8`` precisely so a *byte* offset inside a codepoint exists.
    The offsets are computed from the file here rather than hardcoded, and each
    is proved mid-codepoint before it is used -- a hardcoded offset silently
    drifts onto a boundary the first time the fixture is edited, and the test
    then passes while measuring nothing.
    """
    body = fixture_bytes("fragmented_utf8")
    cuts: list[int] = []
    for sequence in (b"\xc3\xa9", b"\xe6\x97\xa5", b"\xf0\x9d\x84\x9e"):
        found = body.index(sequence)
        for step in range(1, len(sequence)):
            cut = found + step
            # A prefix ending mid-codepoint cannot decode. If this ever stops
            # raising, the cut is on a boundary and proves nothing.
            with pytest.raises(UnicodeDecodeError):
                body[:cut].decode("utf-8")
            cuts.append(cut)

    assert len(cuts) == 1 + 2 + 3
    for cut in cuts:
        assert_invariant(
            "fragmented_utf8", deployed, sizes_from_offsets([cut], len(body))
        )
    # And all of them at once.
    assert_invariant("fragmented_utf8", deployed, sizes_from_offsets(cuts, len(body)))


# ---------------------------------------------------------------------------
# The oracle's own invariance, and the fuzzer's own falsifiability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", FUZZED_FIXTURES)
def test_the_mock_itself_is_content_preserving_under_rechunking(
    fixture_name: str, deployed: Deployment
) -> None:
    """If the oracle changed content when re-chunked, the fuzzer would be measuring it.

    Read directly from the mock, with the gateway out of the picture entirely.
    """
    body = fixture_bytes(fixture_name)
    expected_bytes, expected_semantics = baseline_reductions(fixture_name)
    for sizes in ([1] * len(body), [len(body)], [7] * (len(body) // 7 + 1)):
        direct = run_direct(fixture_name, deployed, rechunk=sizes)
        assert byte_reduction(direct.frames) == expected_bytes
        assert semantic_reduction(direct.frames) == expected_semantics


def test_the_invariance_assertion_can_fail() -> None:
    """A property that has never been shown to go red proves nothing.

    ``assert_invariant`` compares against the fixture's on-disk bytes, so the
    cheapest honest proof that the comparison bites is to hand those bytes a
    difference. Both reductions must reject it.
    """
    expected_bytes, expected_semantics = baseline_reductions("usage_only_final_chunk")
    body = fixture_bytes("usage_only_final_chunk")

    without_usage = b"".join(
        frame for frame in split_frames(body) if b'"usage"' not in frame
    )
    assert byte_reduction([without_usage]) != expected_bytes
    assert semantic_reduction([without_usage]) != expected_semantics

    truncated = body[:-20]
    assert byte_reduction([truncated]) != expected_bytes
    assert semantic_reduction([truncated]) != expected_semantics


def test_sizes_from_offsets_never_produces_a_zero_length_chunk() -> None:
    """The one exclusion, enforced rather than assumed.

    ``set_rechunk`` rejects a zero-length chunk because it is the HTTP chunked
    terminator; a fuzzer that generated one would be reporting a defect in the
    mock. Duplicates, out-of-range values, and unsorted input all have to
    collapse to positive sizes summing to the body length.
    """
    for offsets in ([], [0], [50, 50, 50], [10, 5, 1], [999_999], [1, 2, 3, 99]):
        sizes = sizes_from_offsets(offsets, 100)
        assert all(size > 0 for size in sizes)
        assert sum(sizes) == 100

    # Proof the mock really would refuse one, so the guard above is not
    # protecting against an imaginary rejection.
    mock = MockUpstream()
    try:
        with pytest.raises(ValueError):
            mock.set_rechunk([5, 0, 5])
    finally:
        mock.close()


def test_the_fuzzer_runtime_is_recorded(deployed: Deployment) -> None:
    """Prints the per-example cost the ``MAX_EXAMPLES`` choice is based on."""
    body = fixture_bytes("fragmented_arguments")
    started = time.monotonic()
    rounds = 20
    for index in range(rounds):
        assert_invariant(
            "fragmented_arguments",
            deployed,
            sizes_from_offsets([1 + index * 7], len(body)),
        )
    elapsed = time.monotonic() - started
    print(
        f"\nfuzz cost: {rounds} examples in {elapsed:.2f}s "
        f"({elapsed / rounds * 1000:.1f} ms/example); "
        f"MAX_EXAMPLES={MAX_EXAMPLES} over {len(FUZZED_FIXTURES)} fixtures "
        f"in 2 fuzz tests"
    )
    assert elapsed / rounds < 1.0, "an example got expensive enough to need re-tuning"


def test_the_rechunking_is_observable_end_to_end(deployed: Deployment) -> None:
    """Proof that the fuzzer varies framing rather than repeating one request.

    ``grep -q 'set_rechunk'`` on this file is satisfied by a docstring, and this
    module reaches the primitive indirectly through
    ``run_through_gateway(..., rechunk=...)``. So the real evidence is the count
    of chunks the *client* receives: the same 1311 bytes arrive in 8 pieces under
    the mock's default framing, in 1311 pieces at one byte each, and in 1 piece
    when the whole body is a single chunk -- while the bytes are identical in all
    three. A ``set_rechunk`` that silently stopped working would make these three
    counts equal and this fuzzer would be running the same case 60 times.
    """
    body = fixture_bytes("fragmented_utf8")
    counts: dict[str, int] = {}
    for label, sizes in (
        ("default", None),
        ("one byte per chunk", [1] * len(body)),
        ("whole body", [len(body)]),
    ):
        relayed = run_through_gateway("fragmented_utf8", deployed, rechunk=sizes)
        counts[label] = len(relayed.frames)
        assert relayed.body == body, label

    print(f"\nchunks received by the client per framing: {counts}")
    assert counts["one byte per chunk"] > counts["default"] > counts["whole body"]
    assert counts["one byte per chunk"] == len(body)
    assert counts["whole body"] == 1
