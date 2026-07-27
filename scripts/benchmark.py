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

from hermes_auto.evaluation.baselines import (  # noqa: E402
    build_report,
    render_json,
    render_markdown,
)
from hermes_auto.evaluation.corpus import CorpusError, load_corpus  # noqa: E402


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
    return parser


def main(argv: list[str] | None = None) -> int:
    """Render the report. Returns 0 on success, 2 on a corpus or run error."""
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
        report = build_report(corpus, runs, sources=sources)
    except CorpusError as exc:
        print(f"benchmark: {exc}", file=sys.stderr)
        return 2

    rendered = render_json(report) if args.format == "json" else render_markdown(report)

    if args.output is None:
        sys.stdout.write(rendered)
    else:
        pathlib.Path(args.output).write_text(rendered, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
