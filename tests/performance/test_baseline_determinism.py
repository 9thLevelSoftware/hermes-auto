"""Determinism tests for the fixed-model baseline harness.

These tests measure the **harness**, not the system. No router exists yet, so
there is nothing whose latency could be benchmarked; what is checkable now — and
what design.md §18 Phase 0's exit criterion "baseline tasks run reproducibly"
actually demands — is that the same recorded input always yields the same bytes.

Nothing here calls a provider, opens a socket, or reads a credential.

Eight properties are enforced:

1. Identical inputs render byte-identical output.
2. ``--runs`` argument order does not reach the output.
3. The working directory does not reach the output.
4. No aggregate is ever ``nan`` or ``inf``, including on a zero-success group.
5. A non-finite value arriving *in the input* is raised, never aggregated.
6. Unknown cost is counted, never valued at zero.
7. A count that arrived as an integral float still aggregates as that number.
8. A count that cannot be a count is raised, never coerced to zero.
9. A structurally malformed run raises ``CorpusError``, not ``AttributeError``.

Properties 4 and 5 are two halves of one constraint and only 4 used to hold: 4
is about a ``nan`` this module could manufacture by dividing by zero, 5 about
one handed to it. They need separate tests because the zero-success test in 4
scans rendered output over a zero denominator and so can never observe an
input-side ``nan``.

Properties 5 through 8 are the ones with teeth, and they fail the same way:
quietly, in a report that renders cleanly and compares byte-identical. If 6
regressed, every later phase would treat an unpriced candidate as free and the
design.md §15.4 "at least 20% cost reduction" gate would be measured against a
fiction. If 7 or 8 regressed, ``cached_token_ratio`` — a headline claim of the
project — would read exactly ``0.000000`` with nothing anywhere to say why.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from hermes_auto.evaluation.baselines import (
    BASELINE_STRATEGIES,
    build_report,
    render_json,
    render_markdown,
)
from hermes_auto.evaluation.corpus import CorpusError, load_corpus
from hermes_auto.evaluation.metrics import aggregate

pytestmark = pytest.mark.performance

FIXTURE_SOURCES = ("recorded-run-a.json", "recorded-run-b.json")


# --------------------------------------------------------------------------- #
# Fixtures and synthetic-event helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def baseline_dir(repo_root: pathlib.Path) -> pathlib.Path:
    return repo_root / "tests" / "fixtures" / "baseline"


@pytest.fixture(scope="module")
def corpus(baseline_dir: pathlib.Path):
    return load_corpus(baseline_dir / "corpus.yaml")


@pytest.fixture(scope="module")
def run_a(baseline_dir: pathlib.Path) -> dict:
    return json.loads((baseline_dir / "recorded-run-a.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run_b(baseline_dir: pathlib.Path) -> dict:
    return json.loads((baseline_dir / "recorded-run-b.json").read_text(encoding="utf-8"))


def make_event(event_id: str, **overrides: object) -> dict:
    """Build a minimal schema-shaped outcome event with the five required keys."""
    event: dict[str, object] = {
        "event_id": event_id,
        "event_type": "turn_completed",
        "occurred_at": "2026-01-07T00:00:00Z",
        # 64 lowercase hex characters: the outcome-event schema constrains this
        # field to a digest by pattern, so a synthetic event that is not one
        # would be untestable against the schema it claims to be shaped like.
        "root_session_hash": "0f0e0d0c0b0a0909" * 4,
        "lane_id": "lane-synthetic",
    }
    event.update(overrides)
    return event


def make_run(strategy: str, entries: list[tuple[str, dict]]) -> dict:
    """Wrap ``(task_id, event)`` pairs in the mandatory recorded-run shape.

    ``task_id`` lives beside the event, never inside it: the outcome-event schema
    sets ``additionalProperties: false`` and defines no ``task_id``, so an event
    carrying one could never validate.
    """
    return {
        "strategy": strategy,
        "model_label": "test-model-synthetic",
        "events": [{"task_id": task_id, "event": event} for task_id, event in entries],
    }


# --------------------------------------------------------------------------- #
# Byte-level reproducibility
# --------------------------------------------------------------------------- #


def test_report_is_byte_identical_across_runs(corpus, run_a, run_b) -> None:
    """The phase's reproducibility criterion, made executable."""
    first = render_json(build_report(corpus, [run_a, run_b], sources=FIXTURE_SOURCES))
    second = render_json(build_report(corpus, [run_a, run_b], sources=FIXTURE_SOURCES))

    assert first == second
    assert first.strip(), "render produced empty output"


def test_markdown_render_is_deterministic(corpus, run_a, run_b) -> None:
    first = render_markdown(
        build_report(corpus, [run_a, run_b], sources=FIXTURE_SOURCES)
    )
    second = render_markdown(
        build_report(corpus, [run_a, run_b], sources=FIXTURE_SOURCES)
    )

    assert first == second
    assert first.strip()


def test_report_is_independent_of_run_argument_order(corpus, run_a, run_b) -> None:
    """Argument order must not leak into the output."""
    forward = render_json(build_report(corpus, [run_a, run_b], sources=FIXTURE_SOURCES))
    reverse = render_json(
        build_report(corpus, [run_b, run_a], sources=tuple(reversed(FIXTURE_SOURCES)))
    )

    assert forward == reverse


def test_report_is_independent_of_working_directory(
    corpus, run_a, run_b, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves ``generated_from`` holds basenames, not paths."""
    from_here = render_json(
        build_report(corpus, [run_a, run_b], sources=FIXTURE_SOURCES)
    )

    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    from_elsewhere = render_json(
        build_report(corpus, [run_a, run_b], sources=FIXTURE_SOURCES)
    )

    assert from_here == from_elsewhere
    for source in FIXTURE_SOURCES:
        assert f'"{source}"' in from_here
    assert "tests/fixtures" not in from_here
    assert "tests\\\\fixtures" not in from_here


def test_report_contains_no_volatile_fields(corpus, run_a) -> None:
    """A provenance field would invalidate every stored baseline comparison.

    Blocked by test rather than by convention, because the failure mode is
    silent: the report would still render, and only a later byte-comparison
    against a stored baseline would surface the breakage.
    """
    rendered = render_json(
        build_report(corpus, [run_a], sources=("recorded-run-a.json",))
    )

    for volatile in (
        "generated_at",
        "timestamp",
        "hostname",
        "platform",
        "python_version",
    ):
        assert volatile not in rendered


# --------------------------------------------------------------------------- #
# Cost correctness
# --------------------------------------------------------------------------- #


def test_unknown_cost_is_not_counted_as_zero() -> None:
    """Null cost is unknown; zero cost is free. They must never be conflated.

    This is what prevents a later phase from concluding that the candidate it
    has the least pricing information about is the cheapest one.
    """
    unknown_run = make_run(
        "cheapest-only",
        [
            (
                "bug-fix-null-deref-01",
                make_event("evt_unknown", actual_cost_usd=None, turn_succeeded=True),
            )
        ],
    )
    zero_run = make_run(
        "cheapest-only",
        [
            (
                "bug-fix-null-deref-01",
                make_event("evt_unknown", actual_cost_usd=0.0, turn_succeeded=True),
            )
        ],
    )

    unknown = aggregate([unknown_run])
    zero = aggregate([zero_run])

    assert unknown != zero

    assert unknown.unknown_cost_count == 1
    assert unknown.known_cost_count == 0

    assert zero.known_cost_count == 1
    assert zero.unknown_cost_count == 0

    # Both total 0.0 USD, which is exactly why the counts, not the total, are
    # what distinguishes "we know it cost nothing" from "we do not know".
    assert unknown.total_cost_usd == 0.0
    assert zero.total_cost_usd == 0.0


def test_unknown_cost_is_excluded_from_the_sum() -> None:
    """A null alongside a known cost must not drag the total toward zero."""
    run = make_run(
        "cheapest-only",
        [
            (
                "bug-fix-null-deref-01",
                make_event("evt_known", actual_cost_usd=0.25, turn_succeeded=True),
            ),
            (
                "bug-fix-off-by-one-02",
                make_event("evt_null", actual_cost_usd=None, turn_succeeded=True),
            ),
        ],
    )

    summary = aggregate([run])

    assert summary.total_cost_usd == 0.25
    assert summary.known_cost_count == 1
    assert summary.unknown_cost_count == 1
    # Cost per success is computed over known cost only and is therefore an
    # UNDERSTATEMENT whenever unknown_cost_count is non-zero.
    assert summary.mean_cost_per_succeeded_task == 0.125


# --------------------------------------------------------------------------- #
# Count correctness
# --------------------------------------------------------------------------- #


def test_integral_float_counts_aggregate_as_their_value() -> None:
    """``4200.0`` is the number 4200, not an unreadable value worth zero.

    JSON has one number type, so any recorder that round-trips its counters
    through a float — or through a language whose default number is a double —
    emits these. Schema validation does not catch it and is not meant to:
    ``type: integer`` matches any number with a zero fractional part, so
    ``4200.0`` validates and arrives here intact. Reading them as zero produced
    a report with
    ``cached_token_ratio: 0.000000`` that rendered cleanly, passed every
    determinism check, and was byte-identical on repeat. Invisible by
    construction, which is why it is pinned here.
    """
    run = make_run(
        "cheapest-only",
        [
            (
                "bug-fix-null-deref-01",
                make_event(
                    "evt_float",
                    input_tokens=4200.0,
                    cached_tokens=1800.0,
                    tool_call_count=6.0,
                    invalid_tool_call_count=0.0,
                    turn_succeeded=True,
                ),
            )
        ],
    )

    summary = aggregate([run])

    assert summary.total_input_tokens == 4200
    assert summary.total_cached_tokens == 1800
    assert summary.total_tool_calls == 6
    assert summary.cached_token_ratio == round(1800 / 4200, 6)
    # Narrowed to int, not left as a float that would render as a quoted decimal
    # string in the JSON report and silently change the report's shape.
    assert isinstance(summary.total_input_tokens, int)
    assert not isinstance(summary.total_input_tokens, float)


def test_float_and_int_counts_aggregate_identically() -> None:
    """The serializer that produced a recording must not change its numbers."""
    as_int = make_run(
        "cheapest-only",
        [
            (
                "bug-fix-null-deref-01",
                make_event("evt_n", input_tokens=4200, cached_tokens=1800),
            )
        ],
    )
    as_float = make_run(
        "cheapest-only",
        [
            (
                "bug-fix-null-deref-01",
                make_event("evt_n", input_tokens=4200.0, cached_tokens=1800.0),
            )
        ],
    )

    assert aggregate([as_int]) == aggregate([as_float])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", -500),
        ("cached_tokens", -1),
        ("tool_call_count", -3),
        ("invalid_tool_call_count", -1),
        ("input_tokens", 4200.5),
        ("input_tokens", "4200"),
        ("tool_call_count", True),
        ("cached_tokens", [1800]),
    ],
)
def test_unusable_count_is_raised_not_silently_zeroed(
    field: str, value: object
) -> None:
    """A count that cannot be a count is malformed input, not a measurement of 0.

    This is the distinction that separates a count from a cost. A null
    ``actual_cost_usd`` is *meaningful* — an unpriced local candidate — so it is
    counted and reported. ``input_tokens: -500`` has no reading under which it
    is real: it violates ``type: integer, minimum: 0`` in the outcome-event
    schema. Coercing it to 0 puts a wrong total into a report that renders
    cleanly, so it is raised instead and reaches the operator as exit code 2.
    """
    run = make_run(
        "cheapest-only",
        [("bug-fix-null-deref-01", make_event("evt_bad", **{field: value}))],
    )

    with pytest.raises(CorpusError) as excinfo:
        aggregate([run])

    message = str(excinfo.value)
    assert field in message
    assert "evt_bad" in message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actual_cost_usd", float("nan")),
        ("actual_cost_usd", float("inf")),
        ("actual_cost_usd", float("-inf")),
        ("ttft_ms", float("nan")),
        ("total_latency_ms", float("inf")),
    ],
)
def test_non_finite_measurement_is_raised_not_aggregated(
    field: str, value: float
) -> None:
    """The library half of "no aggregate field is ever ``nan`` or ``inf``".

    ``scripts/benchmark.py`` rejects the ``NaN``/``Infinity`` JSON literals at
    parse time, which covers everything arriving as text. This covers a caller
    that builds event mappings in Python and never goes through JSON at all.

    Schema validation is not a substitute and never could be: ``actual_cost_usd``
    is ``type: number, minimum: 0``, and ``minimum`` does not reject ``nan``
    because ``nan < 0`` evaluates ``False``. A ``nan`` cost previously summed
    straight into ``total_cost_usd`` and rendered as the literal text ``nan``.

    Raised rather than dropped for two distinct reasons. A dropped ``inf`` cost
    yields a total that is wrong but renders cleanly. A ``nan`` in a latency
    sample is worse: ``sorted()`` on a list containing one returns an ordering
    that depends on the input order, so the reported percentile would vary with
    the order the runs happened to be read in — a direct breach of the
    byte-identical criterion this module exists to enforce.
    """
    run = make_run(
        "cheapest-only",
        [("bug-fix-null-deref-01", make_event("evt_nonfinite", **{field: value}))],
    )

    with pytest.raises(CorpusError) as excinfo:
        aggregate([run])

    message = str(excinfo.value)
    assert field in message
    assert "evt_nonfinite" in message


def test_absent_count_is_zero_not_an_error() -> None:
    """Not reported is not malformed.

    Every measurement field is optional in the schema because collection is
    best-effort; an event that omits ``cached_tokens`` contributes nothing and
    must not fail the run.
    """
    run = make_run(
        "cheapest-only",
        [("bug-fix-null-deref-01", make_event("evt_sparse", turn_succeeded=True))],
    )

    summary = aggregate([run])

    assert summary.total_input_tokens == 0
    assert summary.total_cached_tokens == 0
    assert summary.total_tool_calls == 0
    assert summary.cached_token_ratio == 0.0


# --------------------------------------------------------------------------- #
# Structural faults surface as CorpusError, never AttributeError
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "runs"),
    [
        ("run is an array", [[{"strategy": "cheapest-only", "events": []}]]),
        ("run is a string", ["oops"]),
        ("events is a string", [{"strategy": "cheapest-only", "events": "oops"}]),
        ("events is a mapping", [{"strategy": "cheapest-only", "events": {}}]),
        ("entry is a string", [{"strategy": "cheapest-only", "events": ["nope"]}]),
        (
            "event is a string",
            [
                {
                    "strategy": "cheapest-only",
                    "events": [
                        {"task_id": "bug-fix-null-deref-01", "event": "notadict"}
                    ],
                }
            ],
        ),
        (
            "event is absent",
            [
                {
                    "strategy": "cheapest-only",
                    "events": [{"task_id": "bug-fix-null-deref-01"}],
                }
            ],
        ),
    ],
)
def test_malformed_run_raises_corpus_error(corpus, label: str, runs: list) -> None:
    """A malformed run file must be diagnosable as such.

    Each of these shapes previously escaped ``build_report`` as an uncaught
    ``AttributeError`` and exited 1, which an operator cannot distinguish from a
    broken harness. Phase 8 generates these files programmatically and Phase 9+
    diffs reports against Phase 1's, so "your input is wrong" has to be a
    reportable outcome rather than a stack trace.
    """
    with pytest.raises(CorpusError) as excinfo:
        build_report(corpus, runs)

    assert str(excinfo.value), f"{label}: raised an empty message"


def test_malformed_run_error_names_the_offending_index(corpus, run_a) -> None:
    """The index is the whole diagnostic value on a multi-file invocation."""
    with pytest.raises(CorpusError) as excinfo:
        build_report(corpus, [run_a, ["not", "a", "run"]])

    message = str(excinfo.value)
    assert "index 1" in message
    assert "list" in message


def test_malformed_event_error_names_the_entry_position(corpus) -> None:
    run = {
        "strategy": "cheapest-only",
        "events": [
            {"task_id": "bug-fix-null-deref-01", "event": make_event("evt_ok")},
            {"task_id": "bug-fix-off-by-one-02", "event": "notadict"},
        ],
    }

    with pytest.raises(CorpusError) as excinfo:
        build_report(corpus, [run])

    message = str(excinfo.value)
    assert "position 1" in message
    assert "'event'" in message
    assert "str" in message


# --------------------------------------------------------------------------- #
# Aggregation edge cases
# --------------------------------------------------------------------------- #


def test_percentile_on_single_sample_returns_that_sample() -> None:
    run = make_run(
        "cheapest-only",
        [
            (
                "bug-fix-null-deref-01",
                make_event("evt_single", ttft_ms=437, turn_succeeded=True),
            )
        ],
    )

    summary = aggregate([run])

    assert summary.ttft_ms_p50 == 437.0
    assert summary.ttft_ms_p95 == 437.0


def test_empty_runs_produce_zero_report(corpus) -> None:
    report = build_report(corpus, [])

    assert report.task_count == len(corpus)
    assert set(report.strategies) == set(BASELINE_STRATEGIES)
    for summary in report.strategies.values():
        assert summary.event_count == 0
        assert summary.distinct_task_count == 0
        assert summary.total_cost_usd == 0.0
        assert summary.success_rate == 0.0
    assert report.generated_from == ()


def test_unknown_task_id_raises(corpus) -> None:
    run = make_run(
        "cheapest-only",
        [("does-not-exist", make_event("evt_orphan", turn_succeeded=True))],
    )

    with pytest.raises(CorpusError) as excinfo:
        build_report(corpus, [run])

    message = str(excinfo.value)
    assert "does-not-exist" in message
    assert "corpus" in message


def test_unknown_strategy_raises(corpus) -> None:
    run = make_run(
        "not-a-real-strategy",
        [("bug-fix-null-deref-01", make_event("evt_x", turn_succeeded=True))],
    )

    with pytest.raises(CorpusError) as excinfo:
        build_report(corpus, [run])

    assert "not-a-real-strategy" in str(excinfo.value)


def test_every_baseline_strategy_appears_in_report(corpus, run_a) -> None:
    """The report shape is stable even when eight of nine strategies are empty."""
    report = build_report(corpus, [run_a], sources=("recorded-run-a.json",))
    as_json = render_json(report)
    as_markdown = render_markdown(report)

    assert len(BASELINE_STRATEGIES) == 9
    for strategy in BASELINE_STRATEGIES:
        assert strategy in as_json
        assert strategy in as_markdown


def test_zero_success_group_renders_without_nan(corpus) -> None:
    """A group where every turn failed is reachable, so its render must be sane.

    ``succeeded_count`` and ``total_input_tokens`` are both zero here, exercising
    three of the four guarded denominators at once. An unguarded division would
    render the literal text ``nan``, which parses, prints, and silently defeats
    every byte-comparison downstream.
    """
    run = make_run(
        "rule-based-tiers",
        [
            (
                "bug-fix-null-deref-01",
                make_event(
                    "evt_fail_1",
                    event_type="request_failed",
                    turn_succeeded=False,
                    input_tokens=0,
                    cached_tokens=0,
                    tool_call_count=0,
                    actual_cost_usd=None,
                    error_class="timeout",
                ),
            ),
            (
                "bug-fix-off-by-one-02",
                make_event(
                    "evt_fail_2",
                    event_type="request_failed",
                    turn_succeeded=False,
                    input_tokens=0,
                    cached_tokens=0,
                    tool_call_count=0,
                    actual_cost_usd=None,
                    error_class="upstream_5xx",
                ),
            ),
        ],
    )

    summary = aggregate([run])
    assert summary.succeeded_count == 0
    assert summary.total_input_tokens == 0
    assert summary.success_rate == 0.0
    assert summary.mean_cost_per_succeeded_task == 0.0
    assert summary.cached_token_ratio == 0.0
    assert summary.mean_tool_calls_per_task == 0.0

    report = build_report(corpus, [run], sources=("synthetic.json",))
    for rendered in (render_json(report), render_markdown(report)):
        for token in ("nan", "NaN", "Infinity", "inf"):
            assert token not in rendered, f"{token!r} present in rendered output"


def test_metric_summary_reports_all_five_dimensions(corpus, run_a, run_b) -> None:
    """design.md §18 Phase 0 item 5: quality, cost, latency, cache, tool calls."""
    summary = aggregate([run_a, run_b])

    # Quality
    assert summary.event_count == 20
    assert summary.distinct_task_count == 11
    assert 0.0 <= summary.success_rate <= 1.0
    # Cost
    assert summary.total_cost_usd > 0.0
    assert summary.known_cost_count + summary.unknown_cost_count == summary.event_count
    # Latency
    assert summary.ttft_ms_p95 >= summary.ttft_ms_p50
    assert summary.total_latency_ms_p95 >= summary.total_latency_ms_p50
    # Cache
    assert 0.0 <= summary.cached_token_ratio <= 1.0
    # Tool calls
    assert summary.total_tool_calls > 0
    assert summary.mean_tool_calls_per_task > 0.0
