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

Two invocations with the same inputs produce byte-identical output, in any
order, from any working directory. That is the phase's reproducibility
criterion, and it is what lets a Phase 8 report be diffed against a Phase 1 one.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

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
    load_schemas,
    validate,
)

#: ``$id`` of the schema every ``entry["event"]`` is checked against. Only the
#: event is validated, never the entry that wraps it: the schema sets
#: ``additionalProperties: false`` and defines no ``task_id``, so the join key
#: lives beside the event by necessity.
OUTCOME_EVENT_SCHEMA_ID = (
    "https://hermes-auto-router.dev/schema/routing/outcome-event.v1.json"
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
            "Validation is on by default; this exists so a later phase can "
            "profile aggregation without the schema pass in the measurement."
        ),
    )
    return parser


def validate_events(runs: list[dict]) -> None:
    """Validate every ``entry["event"]`` against ``outcome-event.v1``.

    The schema already declares ``type: integer, minimum: 0`` for all six count
    fields and bounded patterns for every string, so this pass rejects a
    malformed recording at the boundary — before any number reaches an
    aggregate — rather than after it has been folded into a total.

    Schemas are loaded once and reused across every event; :func:`validate`
    re-reads the whole schema tree when it is not handed a mapping.

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

    # Checked once, up front, rather than left to surface as a KeyError from
    # validate() on the first event. A missing schema is a property of the
    # installation, so discovering it per-event would be both repetitive and
    # misattributed to whichever event happened to be validated first.
    if OUTCOME_EVENT_SCHEMA_ID not in schemas:
        raise SchemaLoadError(
            f"the packaged schema tree does not carry "
            f"{OUTCOME_EVENT_SCHEMA_ID}; available: {sorted(schemas)}"
        )

    for index, run in enumerate(runs):
        for position, entry in enumerate(run.get("events", ())):
            try:
                validate(entry["event"], OUTCOME_EVENT_SCHEMA_ID, schemas)
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
        ``0`` on success and ``2`` on any bad input — an unreadable or invalid
        corpus, an unreadable or unparseable run file, a structurally malformed
        run, an event that fails ``outcome-event.v1``, an unknown strategy, or
        an unknown ``task_id``. Exit ``2`` is the contract: an operator has to be
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
                runs.append(json.load(handle))
        except (OSError, json.JSONDecodeError) as exc:
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
