#!/usr/bin/env python3
"""Render a fixed-model baseline report from recorded runs.

This is an offline aggregator. It reads a corpus file and one or more
recorded-run JSON files from disk and prints a report. It makes **no network
call**, invokes **no model provider**, and reads **no environment variable and no
credential** — producing the recordings it consumes is Phase 8 work.

Usage::

    PYTHONPATH=src python scripts/benchmark.py \\
        --corpus tests/fixtures/baseline/corpus.yaml \\
        --runs tests/fixtures/baseline/recorded-run-a.json \\
        --runs tests/fixtures/baseline/recorded-run-b.json \\
        --format json

Every recorded event is validated against ``outcome-event.v1`` before anything
is aggregated. Pass ``--no-validate`` to skip that pass. Validation is on by
default because the aggregator's inputs are generated programmatically from
Phase 8 onward, and an unvalidated count field of the wrong type is the kind of
fault that produces a clean-looking report with a wrong number in it.

The non-standard ``NaN``/``Infinity``/``-Infinity`` JSON literals are rejected at
parse time, before validation, because the schema cannot catch them: ``minimum:
0`` does not reject ``NaN``, since ``nan < 0`` is ``False``. See
:func:`_reject_non_finite`.

Two invocations with the same inputs produce byte-identical output, in any
order, from any working directory. That is the phase's reproducibility
criterion, and it is what lets a Phase 8 report be diffed against a Phase 1 one.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import typing

# Allow running straight from a checkout without PYTHONPATH. Derived from
# __file__, never from the working directory or an environment variable, so it
# cannot influence the rendered output.
_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import jsonschema  # noqa: E402

from hermes_auto.evaluation.baselines import (  # noqa: E402
    build_report,
    check_run_shapes,
    render_json,
    render_markdown,
)
from hermes_auto.evaluation.corpus import CorpusError, load_corpus  # noqa: E402
from hermes_auto.gateway.schemas import (  # noqa: E402
    SchemaLoadError,
    build_registry,
    load_schemas,
)

#: ``$id`` of the schema every ``entry["event"]`` is checked against. Only the
#: event is validated, never the entry that wraps it: the schema sets
#: ``additionalProperties: false`` and defines no ``task_id``, so the join key
#: lives beside the event by necessity.
OUTCOME_EVENT_SCHEMA_ID = (
    "https://hermes-auto-router.dev/schema/routing/outcome-event.v1.json"
)


def _reject_non_finite(literal: str) -> typing.NoReturn:
    """Reject the non-standard ``NaN``/``Infinity``/``-Infinity`` JSON literals.

    ``json.load`` accepts all three by default, and nothing downstream stopped
    them. The schema cannot: ``actual_cost_usd`` is ``type: number, minimum: 0``,
    and ``minimum`` does not reject ``NaN`` because ``nan < 0`` evaluates
    ``False``. A recording carrying the bare literal therefore validated cleanly
    and rendered ``total_cost_usd: "nan"`` at exit 0 — falsifying the phase
    constraint that no aggregate field is ever ``nan`` or ``inf``, and defeating
    the byte-comparison that constraint exists to protect. A ``NaN`` reaching
    ``latency_samples`` is worse still: ``sorted()`` on a list containing one is
    input-order-dependent, so the percentile it yields depends on the order the
    runs happened to be read in.

    Hooked here, at parse time, rather than field by field, because one hook
    covers every numeric field at once — including the ones a later revision of
    ``outcome-event.v1`` adds, which a per-field guard would silently not cover.

    Args:
        literal: The offending token exactly as it appeared in the file, which
            is what ``json`` passes to a ``parse_constant`` hook.

    Raises:
        CorpusError: Always. Named so the caller keeps its single "your input is
            bad" exit path; the literal is quoted so an operator can grep the
            file for it.
    """
    raise CorpusError(
        f"contains the non-standard JSON literal {literal}; a recorded run must "
        "carry only finite numbers"
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the CLI parser."""
    parser = argparse.ArgumentParser(
        prog="benchmark.py",
        description=(
            "Aggregate recorded runs into a reproducible fixed-model baseline "
            "report. Reads files only; makes no network call."
        ),
    )
    parser.add_argument(
        "--corpus",
        required=True,
        help="Path to the baseline corpus YAML file.",
    )
    parser.add_argument(
        "--runs",
        required=True,
        action="append",
        metavar="PATH",
        help="Path to a recorded-run JSON file. Repeat for multiple files.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
        help="Output format (default: markdown).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write to this path instead of stdout.",
    )
    parser.add_argument(
        "--no-validate",
        dest="validate",
        action="store_false",
        default=True,
        help=(
            "Skip validating each recorded event against outcome-event.v1. "
            "Validation is on by default and costs well under a millisecond per "
            "event; this exists so a later phase can profile aggregation with "
            "the schema pass out of the measurement entirely, not because the "
            "pass is expensive. It does not relax anything else: a NaN literal, "
            "a negative count, and a structurally malformed run are still "
            "rejected, because an opt-out must not turn a loud failure into a "
            "silently wrong number."
        ),
    )
    return parser


def validate_events(runs: list[dict]) -> None:
    """Validate every ``entry["event"]`` against ``outcome-event.v1``.

    The schema already declares ``type: integer, minimum: 0`` for all six count
    fields and bounded patterns for every string, so this pass rejects a
    malformed recording at the boundary — before any number reaches an
    aggregate — rather than after it has been folded into a total.

    Schemas are loaded once and reused across every event; :func:`load_schemas`
    re-reads the whole schema tree on each call. The **validator** is built once
    too, which is the larger win by far: ``jsonschema.validate`` re-runs
    ``check_schema`` and rebuilds the reference registry on *every* call, paying
    per event for a result that cannot change between events. Measured over 1000
    validations of a fixture event: 12,219 µs/event before, 247 µs/event after —
    a ~50× reduction, or 1222 s against 25 s at the 100k events a later phase is
    expected to aggregate.

    ``check_schema`` still runs, once, below. Dropping it entirely would be
    faster still and wrong: a malformed packaged schema would then surface as a
    confusing per-event validation failure against a bad input file rather than
    as ``jsonschema.SchemaError`` against the installation.

    Args:
        runs: Parsed recorded-run mappings, already shape-checked here.

    Raises:
        CorpusError: A run is structurally malformed, or an event does not
            satisfy the schema. A schema violation is re-raised as
            ``CorpusError`` and not as ``jsonschema.ValidationError`` so the
            caller keeps one exit path for "your input is bad"; the offending
            field path and message are carried through in the text.
        SchemaLoadError: The packaged schema tree is unreadable or does not
            carry ``outcome-event.v1``. A broken installation, not a bad run
            file, and reported as such.
    """
    check_run_shapes(runs)
    schemas = load_schemas()

    # Checked before the subscript below, so a missing schema surfaces as this
    # module's declared error type rather than as a bare KeyError. A missing
    # schema is a property of the installation, not of any one event, and the
    # message says so.
    if OUTCOME_EVENT_SCHEMA_ID not in schemas:
        raise SchemaLoadError(
            f"the packaged schema tree does not carry "
            f"{OUTCOME_EVENT_SCHEMA_ID}; available: {sorted(schemas)}"
        )

    # Built once, outside both loops — see the docstring for the measurement.
    # The validator class is resolved from the schema's own `$schema` rather
    # than hardcoded, so this stays exactly what jsonschema.validate() would
    # have done, minus only the per-call work.
    #
    # NOTE: constructed locally rather than via a `build_validator` helper in
    # hermes_auto.gateway.schemas, which did not exist at the time this was
    # written. If that helper lands, this block collapses to a call to it; the
    # registry construction is the only part duplicated from that module.
    schema = schemas[OUTCOME_EVENT_SCHEMA_ID]
    validator_class = jsonschema.validators.validator_for(schema)
    validator_class.check_schema(schema)
    validator = validator_class(schema, registry=build_registry(schemas))

    for index, run in enumerate(runs):
        for position, entry in enumerate(run.get("events", ())):
            try:
                validator.validate(entry["event"])
            except jsonschema.ValidationError as exc:
                path = "/".join(str(part) for part in exc.absolute_path) or "(root)"
                raise CorpusError(
                    f"recorded run at index {index}, event entry at position "
                    f"{position} (task_id {entry.get('task_id')!r}): event fails "
                    f"outcome-event.v1 at {path}: {exc.message}"
                ) from exc


def main(argv: list[str] | None = None) -> int:
    """Render the report.

    Returns:
        ``0`` on success and ``2`` on any bad input — an unreadable or unparseable
        corpus, an unreadable or unparseable run file, a run carrying a ``NaN`` or
        ``Infinity`` literal, a structurally malformed run, an event that fails
        ``outcome-event.v1``, an unknown strategy, or an unknown ``task_id``.
        Exit ``2`` is the contract: an operator has to be
        able to distinguish "your run file is malformed" from a crashed harness,
        and every one of these used to be reachable as an uncaught traceback.
    """
    args = build_parser().parse_args(argv)

    # Sort the inputs so argument order cannot reach the output. Aggregation
    # sorts again internally; this makes the ordering visible at the boundary
    # too rather than relying on a downstream detail.
    run_paths = sorted(pathlib.Path(path) for path in args.runs)

    try:
        corpus = load_corpus(args.corpus)
    except CorpusError as exc:
        print(f"benchmark: {exc}", file=sys.stderr)
        return 2

    runs: list[dict] = []
    for run_path in run_paths:
        try:
            with run_path.open(encoding="utf-8") as handle:
                # parse_constant fires before any value is stored, so a NaN or
                # Infinity literal is rejected on every path — including
                # --no-validate, which must not be a switch that converts a loud
                # failure into a silently wrong number.
                runs.append(json.load(handle, parse_constant=_reject_non_finite))
        except (OSError, json.JSONDecodeError, CorpusError) as exc:
            print(f"benchmark: {run_path}: {exc}", file=sys.stderr)
            return 2

    # Basenames only. A full path here would make the rendered bytes depend on
    # where the harness was invoked from, defeating byte-comparison.
    sources = tuple(sorted(path.name for path in run_paths))

    try:
        if args.validate:
            validate_events(runs)
        report = build_report(corpus, runs, sources=sources)
    except CorpusError as exc:
        print(f"benchmark: {exc}", file=sys.stderr)
        return 2
    except SchemaLoadError as exc:
        # A broken installation, not a bad run file, so it is reported
        # distinctly rather than folded into the message above. Deliberately
        # NOT catching KeyError alongside it: a stray KeyError from aggregation
        # is a harness bug and must not be mislabelled as a missing schema.
        print(f"benchmark: schema unavailable: {exc}", file=sys.stderr)
        return 2

    rendered = render_json(report) if args.format == "json" else render_markdown(report)

    if args.output is None:
        sys.stdout.write(rendered)
    else:
        pathlib.Path(args.output).write_text(rendered, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
