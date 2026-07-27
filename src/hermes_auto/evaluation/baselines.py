"""The nine required baselines (design.md §15.1) and the comparison report.

Most of these nine strategies have **no recorded runs yet**. Only fixed-model
baselines can be recorded in Phase 1, because the router that would produce the
other seven does not exist: `deterministic-capability-router` arrives in Phase 4,
`capability-plus-learned-residual` in Phase 9, and `oracle-hindsight` can only be
computed retrospectively once several strategies have run over the same corpus.

A strategy with no runs is therefore reported with a **zero summary rather than
omitted**. That is deliberate. The report shape is then constant across phases,
so a Phase 8 report and a Phase 1 report differ only in their numbers — a
diffable comparison instead of a structural one — and a strategy silently
missing from the output cannot be mistaken for a strategy that scored zero.

Report rendering is byte-deterministic by construction:

* Strategy keys and ``generated_from`` entries are sorted.
* ``json.dumps`` runs with ``sort_keys=True`` and a fixed indent.
* Float fields render as fixed-precision six-digit decimal **strings**, never as
  bare floats, so no float repr, ``NaN``, or ``Infinity`` token can reach the
  output.
* ``generated_from`` holds **basenames only**. A path would make the rendered
  bytes depend on the working directory the harness happened to run from.
* The report carries no timestamp, hostname, platform, interpreter version, or
  any other volatile field. Provenance belongs in the surrounding summary
  artifact; a byte-compared report cannot contain anything that changes on its
  own.
"""

from __future__ import annotations

import dataclasses
import json

from hermes_auto.evaluation.corpus import (
    SUPPORTED_CORPUS_VERSION,
    BaselineTask,
    CorpusError,
    require_mapping,
)
from hermes_auto.evaluation.metrics import MetricSummary, aggregate

__all__ = [
    "BASELINE_STRATEGIES",
    "BaselineReport",
    "build_report",
    "check_run_shapes",
    "render_json",
    "render_markdown",
]

#: The nine baselines design.md §15.1 requires every routing claim be compared
#: against, in the order that section lists them.
BASELINE_STRATEGIES: tuple[str, ...] = (
    "cheapest-only",
    "strongest-only",
    "user-fixed-model",
    "random-eligible",
    "rule-based-tiers",
    "deterministic-capability-router",
    "capability-plus-domain-affinity",
    "capability-plus-learned-residual",
    "oracle-hindsight",
)

#: Decimal places used when rendering a float field.
_PRECISION = 6


@dataclasses.dataclass(frozen=True)
class BaselineReport:
    """A full baseline comparison over one corpus.

    Attributes:
        corpus_version: Schema version of the corpus the report covers.
        task_count: ``len(corpus)`` — the number of tasks *defined*. Distinct
            tasks actually *observed* live on each :class:`MetricSummary` as
            ``distinct_task_count``; the two are deliberately different names
            because they are different numbers whenever coverage is partial.
        strategies: Every name in :data:`BASELINE_STRATEGIES`, mapped to its
            summary. Strategies with no recorded runs map to a zero summary.
        generated_from: Sorted **basenames** of the recorded-run files the
            report was built from. Never paths.
    """

    corpus_version: int
    task_count: int
    strategies: dict[str, MetricSummary]
    generated_from: tuple[str, ...]


def check_run_shapes(runs: list[dict]) -> None:
    """Raise :class:`CorpusError` unless every run has the recorded-run shape.

    Checks structure only — that each run is a mapping, that ``events`` is a
    list, and that each entry and its ``event`` are mappings. Field *values* are
    the schema's business, not this function's.

    Split out of :func:`build_report` so a caller that wants to inspect events
    before aggregating them — ``scripts/benchmark.py`` validates each event
    against ``outcome-event.v1`` first — can traverse the structure without
    reimplementing the same guards or risking the ``AttributeError`` they exist
    to prevent. :func:`build_report` calls it too, so aggregating an unchecked
    run is not a reachable mistake.

    Every message names the run index and the entry position, because a
    programmatically generated corpus of recordings is diagnosed by locating the
    bad record, not by learning that one exists.

    Args:
        runs: Parsed recorded-run values, exactly as ``json.load`` returned
            them. The ``list[dict]`` annotation states the *intent*; this
            function is what makes it true, so it must not assume it.

    Raises:
        CorpusError: Any run, entry, or ``event`` is not a mapping, or ``events``
            is present and is not a list.
    """
    for index, raw_run in enumerate(runs):
        run = require_mapping(raw_run, f"recorded run at index {index}")

        raw_events = run.get("events", ())
        # A str is iterable, so an unchecked `"events": "oops"` would iterate
        # characters and fail later with an opaque AttributeError on a str.
        if not isinstance(raw_events, (list, tuple)):
            raise CorpusError(
                f"recorded run at index {index} has 'events' of type "
                f"{type(raw_events).__name__}; expected a list"
            )

        for position, raw_entry in enumerate(raw_events):
            where = f"recorded run at index {index}, event entry at position {position}"
            entry = require_mapping(raw_entry, where)
            require_mapping(entry.get("event"), f"{where}: 'event'")


def build_report(
    corpus: list[BaselineTask],
    runs: list[dict],
    sources: tuple[str, ...] = (),
) -> BaselineReport:
    """Group *runs* by strategy and aggregate each group against *corpus*.

    Args:
        corpus: Tasks from :func:`hermes_auto.evaluation.corpus.load_corpus`.
        runs: Parsed recorded-run mappings. An empty list yields a report with
            zero-valued summaries rather than an error.
        sources: **Basenames** of the files *runs* came from. The parameter
            exists because this function receives parsed dictionaries and has no
            other way to know the filenames; the caller supplies basenames so
            the rendered report does not vary with the working directory.

    Returns:
        A report whose ``strategies`` mapping covers all nine baselines.

    Raises:
        CorpusError: A run is structurally malformed (see
            :func:`check_run_shapes`), declares a ``strategy`` outside
            :data:`BASELINE_STRATEGIES`, names a ``task_id`` the corpus does not
            define, or carries an unusable count field. All are reported by name
            with the offending index. An unknown task id silently dropped would
            understate the denominator of every per-task rate in the report, and
            a structural fault escaping as an ``AttributeError`` would leave an
            operator unable to tell a malformed run file from a broken harness.
    """
    check_run_shapes(runs)

    known_task_ids = {task.task_id for task in corpus}

    grouped: dict[str, list[dict]] = {name: [] for name in BASELINE_STRATEGIES}

    for index, run in enumerate(runs):
        strategy = run.get("strategy")
        # The isinstance test comes first because `unhashable in dict` raises
        # TypeError, which would be a traceback rather than this error.
        if not isinstance(strategy, str) or strategy not in grouped:
            raise CorpusError(
                f"recorded run at index {index} declares unknown strategy "
                f"{strategy!r}; expected one of {list(BASELINE_STRATEGIES)}"
            )

        for position, entry in enumerate(run.get("events", ())):
            task_id = entry.get("task_id")
            if not isinstance(task_id, str) or task_id not in known_task_ids:
                raise CorpusError(
                    f"recorded run at index {index} for strategy {strategy!r} "
                    f"references task_id {task_id!r} at event position "
                    f"{position}, which is absent from the corpus; known task "
                    f"ids are {sorted(known_task_ids)}"
                )

        grouped[strategy].append(run)

    return BaselineReport(
        corpus_version=SUPPORTED_CORPUS_VERSION,
        task_count=len(corpus),
        strategies={name: aggregate(grouped[name]) for name in BASELINE_STRATEGIES},
        generated_from=tuple(sorted(sources)),
    )


def _summary_to_renderable(summary: MetricSummary) -> dict[str, object]:
    """Convert a summary to JSON-ready values with floats as fixed strings."""
    renderable: dict[str, object] = {}
    for field in dataclasses.fields(summary):
        value = getattr(summary, field.name)
        if isinstance(value, float):
            renderable[field.name] = f"{value:.{_PRECISION}f}"
        else:
            renderable[field.name] = value
    return renderable


def render_json(report: BaselineReport) -> str:
    """Render *report* as deterministic JSON.

    Keys are sorted, indentation is fixed, and every float is a six-digit
    decimal string. Two calls with equal input produce byte-identical output.
    """
    document = {
        "corpus_version": report.corpus_version,
        "task_count": report.task_count,
        "generated_from": list(report.generated_from),
        "strategies": {
            name: _summary_to_renderable(report.strategies[name])
            for name in sorted(report.strategies)
        },
    }
    # allow_nan=False is a backstop, not the primary defense: aggregate() already
    # resolves every zero denominator to 0.0. If a non-finite value ever did
    # reach here it would raise loudly instead of emitting the token `NaN`.
    return json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n"


_MARKDOWN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Events", "event_count"),
    ("Tasks seen", "distinct_task_count"),
    ("Succeeded", "succeeded_count"),
    ("Success rate", "success_rate"),
    ("Total cost (USD)", "total_cost_usd"),
    ("Unknown cost", "unknown_cost_count"),
    ("Cost / success", "mean_cost_per_succeeded_task"),
    ("TTFT p50", "ttft_ms_p50"),
    ("TTFT p95", "ttft_ms_p95"),
    ("Latency p50", "total_latency_ms_p50"),
    ("Latency p95", "total_latency_ms_p95"),
    ("Cached ratio", "cached_token_ratio"),
    ("Tool calls", "total_tool_calls"),
    ("Invalid tool calls", "invalid_tool_call_count"),
)


def _cell(summary: MetricSummary, field_name: str) -> str:
    value = getattr(summary, field_name)
    return f"{value:.{_PRECISION}f}" if isinstance(value, float) else str(value)


def render_markdown(report: BaselineReport) -> str:
    """Render *report* as a deterministic Markdown document.

    Same guarantees as :func:`render_json`: sorted strategy rows, fixed-precision
    floats, and no volatile field anywhere in the output.
    """
    sources = ", ".join(report.generated_from) if report.generated_from else "(none)"

    lines: list[str] = [
        "# Fixed-model baseline report",
        "",
        f"- Corpus version: {report.corpus_version}",
        f"- Corpus tasks defined: {report.task_count}",
        f"- Generated from: {sources}",
        "",
        "Unknown costs are counted, never valued at zero; see the "
        "`Unknown cost` column.",
        "",
        "| Strategy | " + " | ".join(label for label, _ in _MARKDOWN_COLUMNS) + " |",
        "| --- | " + " | ".join("---" for _ in _MARKDOWN_COLUMNS) + " |",
    ]

    for name in sorted(report.strategies):
        summary = report.strategies[name]
        cells = [_cell(summary, field_name) for _, field_name in _MARKDOWN_COLUMNS]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    lines.extend(
        [
            "",
            "A strategy with no recorded runs is reported as a zero row rather "
            "than omitted, so the report shape stays constant across phases.",
            "",
        ]
    )
    return "\n".join(lines)
