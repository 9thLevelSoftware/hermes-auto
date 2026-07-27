"""Behavioral tests for ``scripts/benchmark.py``.

The script is the only executable surface of the Phase 1 evaluation harness, and
until these tests existed its argument handling, its error paths, and its exit
codes were proven only by one-off invocations that would never run again.

The module is **imported and called in-process**, not shelled out to. Two
reasons: a subprocess reduces every failure to a return code and a blob of
stderr, and ``scripts/`` is not an installed package, so a subprocess would
depend on ``PYTHONPATH`` being set the way the person running the suite happened
to set it.

The exit code is the contract under test. ``main`` documents ``0`` on success
and ``2`` on any bad input; a structurally malformed run file used to escape as
an uncaught ``AttributeError`` and exit ``1``, which is indistinguishable from a
crashed harness. Every malformed-input case below therefore asserts ``2``
specifically and never merely "non-zero".
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_BASELINE_DIR = _REPO_ROOT / "tests" / "fixtures" / "baseline"
_CORPUS = _BASELINE_DIR / "corpus.yaml"
_RUN_A = _BASELINE_DIR / "recorded-run-a.json"
_RUN_B = _BASELINE_DIR / "recorded-run-b.json"


def _load_benchmark() -> types.ModuleType:
    """Import ``scripts/benchmark.py`` by path.

    Loaded through an explicit spec rather than by inserting ``scripts/`` into
    ``sys.path``, so importing the script under test cannot change what any
    other test in the session imports.
    """
    path = _REPO_ROOT / "scripts" / "benchmark.py"
    spec = importlib.util.spec_from_file_location("hermes_benchmark_script", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


benchmark = _load_benchmark()


def _base_argv(*extra: str) -> list[str]:
    return [
        "--corpus",
        str(_CORPUS),
        "--runs",
        str(_RUN_A),
        "--runs",
        str(_RUN_B),
        *extra,
    ]


@pytest.fixture()
def malformed(tmp_path: pathlib.Path):
    """Return a factory writing *document* to a run file and returning its path."""

    def write(document: object, name: str = "malformed.json") -> pathlib.Path:
        path = tmp_path / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    return write


def _run_with(path: pathlib.Path, *extra: str) -> list[str]:
    return ["--corpus", str(_CORPUS), "--runs", str(path), *extra]


# --------------------------------------------------------------------------- #
# Success paths
# --------------------------------------------------------------------------- #


def test_exits_zero_on_the_real_fixtures(capsys: pytest.CaptureFixture[str]) -> None:
    assert benchmark.main(_base_argv("--format", "json")) == 0

    document = json.loads(capsys.readouterr().out)
    assert sorted(document["generated_from"]) == [
        "recorded-run-a.json",
        "recorded-run-b.json",
    ]
    assert document["task_count"] == 11


def test_markdown_is_the_default_format(capsys: pytest.CaptureFixture[str]) -> None:
    assert benchmark.main(_base_argv()) == 0

    out = capsys.readouterr().out
    assert out.startswith("# Fixed-model baseline report")
    assert "| strongest-only |" in out


def test_output_flag_writes_to_a_file(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--output`` must divert the report, not duplicate it to stdout."""
    destination = tmp_path / "nested" / "report.json"
    destination.parent.mkdir()

    argv = _base_argv("--format", "json", "--output", str(destination))

    assert benchmark.main(argv) == 0

    written = destination.read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""
    assert json.loads(written)["task_count"] == 11


def test_runs_argument_order_does_not_reach_the_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reproducibility contract, asserted at the CLI boundary."""
    assert benchmark.main(_base_argv("--format", "json")) == 0
    forward = capsys.readouterr().out

    reversed_argv = [
        "--corpus",
        str(_CORPUS),
        "--runs",
        str(_RUN_B),
        "--runs",
        str(_RUN_A),
        "--format",
        "json",
    ]
    assert benchmark.main(reversed_argv) == 0
    reverse = capsys.readouterr().out

    assert forward == reverse
    assert forward.strip()


def test_no_validate_still_renders_the_same_bytes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validation is a gate, not an input to the report.

    If skipping it changed a single byte, the opt-out would silently produce a
    second, incomparable flavor of report.
    """
    assert benchmark.main(_base_argv("--format", "json")) == 0
    validated = capsys.readouterr().out

    assert benchmark.main(_base_argv("--format", "json", "--no-validate")) == 0
    unvalidated = capsys.readouterr().out

    assert validated == unvalidated


# --------------------------------------------------------------------------- #
# Malformed run files: exit 2, never a traceback
# --------------------------------------------------------------------------- #


def test_run_file_that_is_a_json_array_exits_two(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reproduces the ``'list' object has no attribute 'get'`` traceback."""
    path = malformed([{"strategy": "cheapest-only", "events": []}])

    assert benchmark.main(_run_with(path)) == 2

    err = capsys.readouterr().err
    assert "must be a mapping" in err
    assert "list" in err
    assert "index 0" in err


def test_run_file_with_non_list_events_exits_two(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reproduces the ``'str' object has no attribute 'get'`` traceback.

    A str is iterable, so the unguarded loop iterated its characters instead of
    rejecting it.
    """
    path = malformed({"strategy": "cheapest-only", "events": "oops"})

    assert benchmark.main(_run_with(path)) == 2

    err = capsys.readouterr().err
    assert "'events'" in err
    assert "str" in err


def test_run_file_with_non_mapping_event_exits_two(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reproduces the ``pair[1].get("event_id")`` traceback in metrics."""
    path = malformed(
        {
            "strategy": "cheapest-only",
            "events": [{"task_id": "bug-fix-null-deref-01", "event": "notadict"}],
        }
    )

    assert benchmark.main(_run_with(path)) == 2

    err = capsys.readouterr().err
    assert "'event'" in err
    assert "must be a mapping" in err
    assert "position 0" in err


def test_run_file_with_non_mapping_entry_exits_two(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    path = malformed({"strategy": "cheapest-only", "events": ["notadict"]})

    assert benchmark.main(_run_with(path)) == 2
    assert "must be a mapping" in capsys.readouterr().err


@pytest.mark.parametrize("shape", [[], "oops", 42, None])
def test_top_level_non_object_run_exits_two(
    malformed, capsys: pytest.CaptureFixture[str], shape: object
) -> None:
    """No JSON top-level value other than an object may reach an aggregate."""
    path = malformed(shape)

    assert benchmark.main(_run_with(path)) == 2
    assert capsys.readouterr().err.startswith("benchmark: ")


# --------------------------------------------------------------------------- #
# Semantic errors: also exit 2
# --------------------------------------------------------------------------- #


def test_unknown_task_id_exits_two(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dropped task id would understate every per-task denominator."""
    path = malformed(
        {
            "strategy": "cheapest-only",
            "events": [
                {
                    "task_id": "does-not-exist",
                    "event": {
                        "event_id": "evt_orphan",
                        "event_type": "turn_completed",
                        "occurred_at": "2026-01-07T00:00:00Z",
                        "root_session_hash": "0" * 64,
                        "lane_id": "lane-orphan",
                    },
                }
            ],
        }
    )

    assert benchmark.main(_run_with(path)) == 2

    err = capsys.readouterr().err
    assert "does-not-exist" in err
    assert "corpus" in err


def test_unknown_strategy_exits_two(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    path = malformed({"strategy": "not-a-real-strategy", "events": []})

    assert benchmark.main(_run_with(path)) == 2
    assert "not-a-real-strategy" in capsys.readouterr().err


def test_missing_run_file_exits_two(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert benchmark.main(_run_with(tmp_path / "absent.json")) == 2
    assert "absent.json" in capsys.readouterr().err


def test_unparseable_run_file_exits_two(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    assert benchmark.main(_run_with(path)) == 2
    assert "broken.json" in capsys.readouterr().err


def test_missing_corpus_exits_two(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--corpus", str(tmp_path / "absent.yaml"), "--runs", str(_RUN_A)]

    assert benchmark.main(argv) == 2
    assert "absent.yaml" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Schema validation (on by default)
# --------------------------------------------------------------------------- #


def _run_with_event(**overrides: object) -> dict:
    event: dict[str, object] = {
        "event_id": "evt_validate",
        "event_type": "turn_completed",
        "occurred_at": "2026-01-07T00:00:00Z",
        "root_session_hash": "0" * 64,
        "lane_id": "lane-validate",
    }
    event.update(overrides)
    return {
        "strategy": "cheapest-only",
        "events": [{"task_id": "bug-fix-null-deref-01", "event": event}],
    }


def test_schema_violation_exits_two_by_default(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    """``minimum: 0`` in the schema is what catches a negative count first."""
    path = malformed(_run_with_event(input_tokens=-500))

    assert benchmark.main(_run_with(path)) == 2

    err = capsys.readouterr().err
    assert "outcome-event.v1" in err
    assert "input_tokens" in err


def test_schema_violation_names_the_offending_entry(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    path = malformed(_run_with_event(root_session_hash="not-a-digest"))

    assert benchmark.main(_run_with(path)) == 2

    err = capsys.readouterr().err
    assert "root_session_hash" in err
    assert "position 0" in err
    assert "bug-fix-null-deref-01" in err


def test_no_validate_opts_out_of_the_schema_pass(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    """The opt-out must actually skip the schema, and only the schema.

    ``retry_count: "many"`` is a type error the schema rejects but that no
    aggregate reads, so with ``--no-validate`` the report renders. A count the
    aggregator *does* read stays guarded either way — see the next test.
    """
    path = malformed(_run_with_event(retry_count="many"))

    assert benchmark.main(_run_with(path)) == 2
    assert "retry_count" in capsys.readouterr().err

    assert benchmark.main(_run_with(path, "--no-validate")) == 0
    assert capsys.readouterr().out.strip()


def test_negative_count_is_rejected_even_with_no_validate(
    malformed, capsys: pytest.CaptureFixture[str]
) -> None:
    """Defense in depth: aggregation refuses a bad count on its own.

    Without this, ``--no-validate`` would be a switch that turns a loud failure
    into a silently wrong number.
    """
    path = malformed(_run_with_event(input_tokens=-500))

    assert benchmark.main(_run_with(path, "--no-validate")) == 2
    assert "cannot be negative" in capsys.readouterr().err


def test_missing_schema_is_reported_not_traced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken install exits 2 with a distinct message, not a ``KeyError``.

    The message is deliberately not the malformed-run wording: an operator whose
    package is missing its schema tree must not spend time re-checking their
    input files.
    """
    monkeypatch.setattr(benchmark, "load_schemas", dict)

    assert benchmark.main(_base_argv()) == 2

    err = capsys.readouterr().err
    assert "schema unavailable" in err
    assert benchmark.OUTCOME_EVENT_SCHEMA_ID in err


@pytest.mark.parametrize("extra", [(), ("--no-validate",)])
def test_float_counts_aggregate_through_the_cli(
    malformed, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...]
) -> None:
    """A serializer that round-trips counts through floats must still aggregate.

    Validation does **not** catch this and is not meant to: JSON Schema's
    ``integer`` type matches any number with a zero fractional part, so
    ``4200.0`` is a valid ``input_tokens`` and reaches the aggregator on the
    default path. That is precisely why the schema pass is not a substitute for
    the guard in ``metrics._as_count`` — parametrized over both paths here to
    keep that from being re-argued.

    Reading these as zero produced ``cached_token_ratio: 0.000000`` in a report
    that rendered cleanly and compared byte-identical on repeat.
    """
    path = malformed(_run_with_event(input_tokens=4200.0, cached_tokens=1800.0))

    assert benchmark.main(_run_with(path, "--format", "json", *extra)) == 0

    summary = json.loads(capsys.readouterr().out)["strategies"]["cheapest-only"]
    assert summary["total_input_tokens"] == 4200
    assert summary["total_cached_tokens"] == 1800
    assert summary["cached_token_ratio"] == "0.428571"
