"""Deterministic aggregation of recorded runs into baseline metrics.

This module reads *already recorded* outcome events and reduces them to the five
dimensions design.md §18 Phase 0 item 5 requires — quality, cost, latency,
cache, and tool calls. It calls no model provider, opens no socket, and reads no
credential; producing recordings against real providers is Phase 8 work.

Input shape
-----------
A *run* is a mapping with a ``strategy``, an optional ``model_label``, and an
``events`` list. Each entry of ``events`` is exactly::

    {"task_id": "<corpus task id>", "event": { ...outcome event... }}

The wrapper is not a stylistic choice. ``outcome-event.v1.schema.json`` sets
``additionalProperties: false`` and defines no ``task_id``, so a ``task_id``
placed *inside* an event can never validate. The join key therefore lives beside
the event, and only ``entry["event"]`` is ever validated against the schema.

Three invariants
----------------
**Unknown cost is never zero.** ``actual_cost_usd`` is nullable and null means
*unknown* — an unpriced local candidate, or a provider that did not report a
figure. Only known values are summed into :attr:`MetricSummary.total_cost_usd`;
everything else is counted into :attr:`MetricSummary.unknown_cost_count`. This
mirrors design.md §7.5 and is the single most important property in this module:
without it a later phase could conclude that the candidate it knows least about
is free.

**No count is ever silently zero.** An absent count field means *not reported*
and contributes 0, which is correct: the schema makes every measurement
optional because collection is best-effort. A count that is *present but
unusable* — negative, boolean, non-integral, or not a number at all — is not a
measurement of zero, it is malformed input, and :func:`aggregate` raises
:class:`~hermes_auto.evaluation.corpus.CorpusError` naming the field and the
value. Integral floats such as ``4200.0`` are accepted and narrowed to ``int``,
because any serializer that round-trips a count through a JSON number produces
them and they carry the measurement exactly. Schema validation is not a
substitute for this: JSON Schema's ``integer`` type matches any number with a
zero fractional part, so ``4200.0`` satisfies ``outcome-event.v1`` and arrives
here regardless. The failure this prevents is
invisible by construction: a recorder emitting float token counts that were
coerced to zero would render a ``cached_token_ratio`` of ``0.000000`` in a
report that passes every determinism check and is byte-identical on repeat.

**No field is ever ``nan`` or ``inf``.** This has two halves, and only the
first used to be true.

*Division.* Every ratio and mean divides by a denominator that can legitimately
be zero — a group of entirely failed turns has no successes, and a fully
cache-free group has no input tokens. Each such expression yields ``0.0``
instead. ``f"{float('nan'):.6f}"`` renders the literal text ``nan``, which would
defeat the byte-comparison that is this phase's reproducibility criterion.

*Input.* A non-finite value can also arrive from outside, and schema validation
does not stop it: ``actual_cost_usd`` is ``type: number, minimum: 0``, and
``minimum`` does not reject ``nan`` because ``nan < 0`` is ``False``. Such a
value is raised by :func:`_as_measure`, not dropped — dropping an ``inf`` cost
would produce a total that renders cleanly and is wrong, and a ``nan`` in a
latency sample makes ``sorted()`` input-order-dependent, so the reported
percentile would depend on the order the runs were read in. On the CLI path the
``NaN``/``Infinity`` JSON literals never get this far; ``scripts/benchmark.py``
rejects them at parse time.
"""

from __future__ import annotations

import dataclasses
import math

from hermes_auto.evaluation.corpus import CorpusError

__all__ = ["MetricSummary", "aggregate"]

#: Decimal places every float field is rounded to before it is stored. Fixed
#: precision is what makes repeated aggregation byte-identical downstream.
_PRECISION = 6


@dataclasses.dataclass(frozen=True)
class MetricSummary:
    """Aggregated baseline metrics for one group of recorded runs.

    Fields are grouped by the five dimensions design.md §18 Phase 0 item 5 names.

    Quality:
        event_count: Number of events aggregated.
        distinct_task_count: Number of distinct ``task_id`` values seen. This is
            the denominator for per-task rates; it is never called
            ``task_count``, which on :class:`~hermes_auto.evaluation.baselines.BaselineReport`
            means the size of the corpus instead.
        succeeded_count: Number of distinct tasks with at least one event
            reporting ``turn_succeeded: true``.
        success_rate: ``succeeded_count / distinct_task_count``, or ``0.0``.
        invalid_tool_call_count: Sum of ``invalid_tool_call_count``.
        empty_response_count: Number of events flagged ``empty_response: true``.

    Economics:
        total_cost_usd: Sum of **known** ``actual_cost_usd`` values only.
        known_cost_count: Events carrying a numeric ``actual_cost_usd``.
        unknown_cost_count: Events whose cost is null or absent. Never folded
            into ``total_cost_usd`` as zero.
        mean_cost_per_succeeded_task: ``total_cost_usd / succeeded_count``, or
            ``0.0``. Understates true cost whenever ``unknown_cost_count`` is
            non-zero, which is why that count is reported alongside it.

    Performance:
        ttft_ms_p50, ttft_ms_p95: Nearest-rank percentiles of ``ttft_ms``.
        total_latency_ms_p50, total_latency_ms_p95: Nearest-rank percentiles of
            ``total_latency_ms``.

    Cache:
        total_input_tokens: Sum of ``input_tokens``.
        total_cached_tokens: Sum of ``cached_tokens``.
        cached_token_ratio: ``total_cached_tokens / total_input_tokens``, or
            ``0.0``.

    Tool calls:
        total_tool_calls: Sum of ``tool_call_count``.
        mean_tool_calls_per_task: ``total_tool_calls / distinct_task_count``, or
            ``0.0``.
    """

    # Quality
    event_count: int = 0
    distinct_task_count: int = 0
    succeeded_count: int = 0
    success_rate: float = 0.0
    invalid_tool_call_count: int = 0
    empty_response_count: int = 0

    # Economics
    total_cost_usd: float = 0.0
    known_cost_count: int = 0
    unknown_cost_count: int = 0
    mean_cost_per_succeeded_task: float = 0.0

    # Performance
    ttft_ms_p50: float = 0.0
    ttft_ms_p95: float = 0.0
    total_latency_ms_p50: float = 0.0
    total_latency_ms_p95: float = 0.0

    # Cache
    total_input_tokens: int = 0
    total_cached_tokens: int = 0
    cached_token_ratio: float = 0.0

    # Tool calls
    total_tool_calls: int = 0
    mean_tool_calls_per_task: float = 0.0


def _safe_ratio(numerator: float, denominator: float) -> float:
    """Return ``numerator / denominator``, or ``0.0`` when the denominator is 0.

    Every division in this module goes through here. A zero denominator is a
    reachable state, not a theoretical one: a group in which every turn failed
    has ``succeeded_count == 0``, and a group with no reported usage has
    ``total_input_tokens == 0``.
    """
    if not denominator:
        return 0.0
    result = numerator / denominator
    if math.isnan(result) or math.isinf(result):
        return 0.0
    return result


def _percentile(values: list[float], percent: float) -> float:
    """Nearest-rank percentile of *values*.

    An empty input yields ``0.0``; a single sample yields that sample, which is
    the behavior the baseline harness relies on when a task was recorded once.
    Nearest-rank is used in preference to an interpolating estimator because it
    always returns an observed value and needs no tie-breaking rule, so repeated
    runs cannot diverge in the last bits of a float.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(percent / 100.0 * len(ordered))
    index = min(max(rank, 1), len(ordered)) - 1
    return float(ordered[index])


def _as_count(value: object, field: str, event_id: str) -> int:
    """Return *value* as a non-negative count.

    Args:
        value: The raw value read from an outcome event. ``None`` and an absent
            key both arrive here as ``None``.
        field: Name of the event field, used in the error message.
        event_id: ``event_id`` of the owning event, used in the error message.

    Returns:
        ``0`` when the field was not reported, otherwise the count. An integral
        float is narrowed to :class:`int`: ``4200.0`` is the number 4200, and a
        recorder that serialized its counters through a float must not be read
        as having measured nothing.

    Raises:
        CorpusError: *value* is present but cannot be a count — negative, a
            bool, a non-integral float, or a non-number. Every one of these
            violates ``minimum: 0`` / ``type: integer`` in
            ``outcome-event.v1.schema.json``, so none of them is a state a
            well-formed recording can reach. Returning 0 instead would put a
            wrong total into a report that renders cleanly and compares
            byte-identical, which is precisely the defect this guard exists to
            make impossible.
    """
    if value is None:
        return 0

    if isinstance(value, bool):
        raise CorpusError(
            f"event {event_id!r} has {field} {value!r}; expected a non-negative "
            "integer, not a boolean"
        )

    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise CorpusError(
                f"event {event_id!r} has {field} {value!r}; a count must be a "
                "whole number"
            )
        value = int(value)

    if not isinstance(value, int):
        raise CorpusError(
            f"event {event_id!r} has {field} {value!r} of type "
            f"{type(value).__name__}; expected a non-negative integer"
        )

    if value < 0:
        raise CorpusError(
            f"event {event_id!r} has {field} {value}; a count cannot be negative"
        )

    return value


def _as_measure(value: object, field: str, event_id: str) -> float | None:
    """Return *value* as a finite measurement, or ``None`` when not reported.

    The counterpart of :func:`_as_count` for the three fields that are genuinely
    real-valued — ``actual_cost_usd``, ``ttft_ms``, and ``total_latency_ms``.

    Args:
        value: The raw value read from an outcome event.
        field: Name of the event field, used in the error message.
        event_id: ``event_id`` of the owning event, used in the error message.

    Returns:
        ``None`` when the measurement is absent, null, a bool, or not a number
        at all — every one of which means *not reported*, never zero. Otherwise
        the value as a :class:`float`.

    Raises:
        CorpusError: *value* is a number but is ``nan``, ``inf``, or ``-inf``.
            ``scripts/benchmark.py`` rejects the ``NaN``/``Infinity`` JSON
            literals at parse time, which covers every field at once for CLI
            input; this covers the library path, where a caller building event
            mappings in Python can produce a non-finite float with no JSON text
            involved. Both are needed, because the schema cannot help: ``minimum:
            0`` does not reject ``nan``, since ``nan < 0`` is ``False``.

            Raised rather than dropped, for the same reason a bad count is: a
            silently discarded ``inf`` cost yields a total that renders cleanly
            and is wrong. A ``nan`` that reaches a latency sample is worse than
            wrong — ``sorted()`` on a list containing one returns an
            input-order-dependent ordering, so the percentile would depend on
            the order the runs were read in, breaking the reproducibility
            criterion outright.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    if not math.isfinite(value):
        raise CorpusError(
            f"event {event_id!r} has {field} {value!r}; a measurement must be a "
            "finite number"
        )

    return float(value)


def _sorted_entries(runs: list[dict]) -> list[tuple[str, dict]]:
    """Flatten *runs* into ``(task_id, event)`` pairs in a deterministic order.

    Sorted by ``(task_id, event_id)`` rather than left in parsed-JSON order, so
    neither file order nor the order the caller passed ``--runs`` can influence
    the sequence in which floats are summed.
    """
    pairs: list[tuple[str, dict]] = []
    for run in runs:
        for entry in run.get("events", ()):
            task_id = entry.get("task_id", "")
            event = entry.get("event") or {}
            pairs.append((str(task_id), event))
    return sorted(pairs, key=lambda pair: (pair[0], str(pair[1].get("event_id", ""))))


def aggregate(runs: list[dict]) -> MetricSummary:
    """Reduce *runs* to a single :class:`MetricSummary`.

    Args:
        runs: Recorded-run mappings. An empty list is valid and produces a
            zero-valued summary rather than raising — an empty baseline is a
            reportable state, not an error.

    Returns:
        The aggregated summary. Every float field is rounded to six decimal
        places, and no field is ever ``nan`` or ``inf``.

    Raises:
        CorpusError: An event carries a count field that is present but not a
            non-negative whole number, or a measurement field that is present
            but not finite. Reported rather than coerced to zero or dropped; see
            the module docstring.
    """
    entries = _sorted_entries(runs)

    task_ids: set[str] = set()
    succeeded_tasks: set[str] = set()
    invalid_tool_calls = 0
    empty_responses = 0
    total_cost = 0.0
    known_cost_count = 0
    unknown_cost_count = 0
    ttft_samples: list[float] = []
    latency_samples: list[float] = []
    total_input_tokens = 0
    total_cached_tokens = 0
    total_tool_calls = 0

    for task_id, event in entries:
        task_ids.add(task_id)
        event_id = str(event.get("event_id", ""))

        if event.get("turn_succeeded") is True:
            succeeded_tasks.add(task_id)

        invalid_tool_calls += _as_count(
            event.get("invalid_tool_call_count"), "invalid_tool_call_count", event_id
        )
        if event.get("empty_response") is True:
            empty_responses += 1

        # Unknown cost is counted, never valued at zero. `None` and an absent
        # key are both unknown: a missing measurement is not evidence of a free
        # turn.
        cost = _as_measure(event.get("actual_cost_usd"), "actual_cost_usd", event_id)
        if cost is None:
            unknown_cost_count += 1
        else:
            total_cost += cost
            known_cost_count += 1

        ttft = _as_measure(event.get("ttft_ms"), "ttft_ms", event_id)
        if ttft is not None:
            ttft_samples.append(ttft)
        latency = _as_measure(
            event.get("total_latency_ms"), "total_latency_ms", event_id
        )
        if latency is not None:
            latency_samples.append(latency)

        total_input_tokens += _as_count(
            event.get("input_tokens"), "input_tokens", event_id
        )
        total_cached_tokens += _as_count(
            event.get("cached_tokens"), "cached_tokens", event_id
        )
        total_tool_calls += _as_count(
            event.get("tool_call_count"), "tool_call_count", event_id
        )

    distinct_task_count = len(task_ids)
    succeeded_count = len(succeeded_tasks)

    return MetricSummary(
        event_count=len(entries),
        distinct_task_count=distinct_task_count,
        succeeded_count=succeeded_count,
        success_rate=round(
            _safe_ratio(succeeded_count, distinct_task_count), _PRECISION
        ),
        invalid_tool_call_count=invalid_tool_calls,
        empty_response_count=empty_responses,
        total_cost_usd=round(total_cost, _PRECISION),
        known_cost_count=known_cost_count,
        unknown_cost_count=unknown_cost_count,
        mean_cost_per_succeeded_task=round(
            _safe_ratio(total_cost, succeeded_count), _PRECISION
        ),
        ttft_ms_p50=round(_percentile(ttft_samples, 50.0), _PRECISION),
        ttft_ms_p95=round(_percentile(ttft_samples, 95.0), _PRECISION),
        total_latency_ms_p50=round(_percentile(latency_samples, 50.0), _PRECISION),
        total_latency_ms_p95=round(_percentile(latency_samples, 95.0), _PRECISION),
        total_input_tokens=total_input_tokens,
        total_cached_tokens=total_cached_tokens,
        cached_token_ratio=round(
            _safe_ratio(total_cached_tokens, total_input_tokens), _PRECISION
        ),
        total_tool_calls=total_tool_calls,
        mean_tool_calls_per_task=round(
            _safe_ratio(total_tool_calls, distinct_task_count), _PRECISION
        ),
    )
