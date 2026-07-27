# Plan 01-07 Summary — Baseline Corpus & Measurement Harness

**Status**: Complete
**Phase**: 01-contracts-schemas-baselines, Wave 3 (final plan of Phase 1)
**Executed**: 2026-07-26
**Requirements**: R26 (primary — baseline corpus and reproducibility surface), R21 (harness scaffolding)
**Interpreter**: `C:\Users\dasbl\hermes-auto\.venv\Scripts\python.exe` (Python 3.11.15)
**design.md blob at execution**: `18bb54b36485fa0813ec67f84a74628a9eee3aae` — matches the `01-CONTEXT.md`
pin, so every `§N` citation below refers to the section intended at planning time.
`git diff --exit-code design.md` exits 0.

Provenance lives here deliberately. `BaselineReport` carries **no** timestamp, hostname, platform, or
interpreter-version field, because it is byte-compared; anything that changes on its own would
invalidate every stored comparison. This file is where that information belongs.

---

## Final corpus: 11 tasks across all nine categories

`tests/fixtures/baseline/corpus.yaml`, `version: 1`, 11 tasks (plan floor was 10).

| Category (design.md §15.3) | Tasks | `task_id` |
|---|---|---|
| `bug_fixing` | 2 | `bug-fix-null-deref-01`, `bug-fix-off-by-one-02` |
| `feature_implementation` | 2 | `feature-add-cli-flag-01`, `feature-add-retry-policy-02` |
| `test_repair` | 1 | `test-repair-fixture-drift-01` |
| `refactoring` | 1 | `refactor-extract-module-01` |
| `multi_step_shell` | 1 | `shell-multi-step-pipeline-01` |
| `web_research` | 1 | `web-research-api-survey-01` |
| `data_extraction` | 1 | `data-extract-structured-01` |
| `vision_assisted` | 1 | `vision-assisted-layout-01` |
| `context_compression` | 1 | `context-compress-long-thread-01` |

**Verifiability**: 8 of 11 are `verifiable: true` (floor was 6). The three judged tasks are
`web-research-api-survey-01`, `vision-assisted-layout-01`, and `context-compress-long-thread-01`.
They were kept rather than dropped because excluding them would bias the corpus toward exactly the
work a cheap model finds easiest — which would flatter any future routing claim.

The two categories doubled up (`bug_fixing`, `feature_implementation`) are the two the dynamic
execution track will exercise most heavily. `context-compress-long-thread-01` is the one task with
`expected_tool_calls: null`, because no meaningful tool-call expectation exists for a compression
scenario.

Every description is a content-free abstraction. `load_corpus` returns tasks **sorted by `task_id`**,
which is where report determinism begins: reordering the YAML cannot change rendered output.

---

## Fixture conformance: proven, not asserted

All **20 events** across both recorded-run fixtures validate against plan 01-04's
`outcome-event.v1.schema.json` through plan 01-03's `validate()`:

```
$ PYTHONPATH=src python -c "import json; from hermes_auto.gateway.schemas import validate; \
    [[validate(e['event'],'https://hermes-auto-router.dev/schema/routing/outcome-event.v1.json') \
      for e in json.load(open(f))['events']] \
     for f in ('tests/fixtures/baseline/recorded-run-a.json','tests/fixtures/baseline/recorded-run-b.json')]"
all events validate
```

**The wrapper shape used everywhere, with no conditional branch anywhere in the code:**

```json
{"task_id": "<corpus task id>", "event": { /* outcome-event object */ }}
```

`task_id` lives **outside** the event because the schema sets `additionalProperties: false` and
defines no such property. `aggregate()` reads metrics from `entry["event"]` and joins on
`entry["task_id"]`; `validate()` is only ever called on `entry["event"]`. The same shape is used by
both fixtures and by every inline synthetic dict in the determinism test (`make_run()` constructs
it).

**Loader usage per 01-03's handoff**: `load_schemas()` does not cache. The harness itself never calls
the loader — schema conformance is a fixture-authoring gate, not a runtime path — so no hot loop
re-reads schema files. Where verification commands validate in a loop, the plan's own command form
was used verbatim.

Fixture composition, deliberately exercising every aggregation branch:

| Case | Where |
|---|---|
| `actual_cost_usd: null` (unknown price) | run-a ×2, run-b ×5 |
| `turn_succeeded: false` | run-a `evt_a_0006`, run-b `evt_b_0005` |
| `invalid_tool_call_count > 0` | run-a ×3 (values 2, 1, 1), run-b ×2 (1, 3) |
| `cached_tokens > 0` | run-a ×6, run-b ×6 |
| `cached_tokens: 0` | run-a ×4, run-b ×4 |
| `empty_response: true` | one per run, on the failed turn |
| `feedback` enum | `good` (run-a), `bad` (run-b) |

run-a is `strategy: strongest-only`; run-b is `strategy: user-fixed-model`. Two populated strategy
groups and seven zero groups, so the "zero row rather than omitted" behavior is exercised by the
default fixtures rather than only by a synthetic test.

**Privacy scan clean.** No prompt text, code, diff, real path, hostname, or credential in any
baseline fixture. Verified by pattern scan for `sk-*`, `X:\`, `/home/`, `/Users/`, `api_key`,
`password`, and PEM headers across all three baseline fixtures, plus the plan's
`"(prompt|raw_prompt|messages|content)"` grep. Candidate identifiers are the synthetic
`test-hosted-general` / `test-local-fast` / `test-coding-specialist` set already established by
plan 01-04.

---

## The correctness properties that matter

**Unknown cost is never zero.** `actual_cost_usd` null — *or absent* — increments
`unknown_cost_count`; only numeric values are summed into `total_cost_usd`. An absent key is treated
as unknown on the same reasoning: a missing measurement is not evidence of a free turn. The invariant
`known_cost_count + unknown_cost_count == event_count` holds by construction.
`test_unknown_cost_is_not_counted_as_zero` proves a null-cost summary and a zero-cost summary are
**not equal** even though both total `0.0` USD — the counts, not the total, are what distinguish "we
know it was free" from "we do not know". A second test proves a null alongside a known cost does not
drag the sum toward zero.

Consequence recorded in `docs/evaluation.md` for every downstream consumer: whenever
`unknown_cost_count` is non-zero, `total_cost_usd` and `mean_cost_per_succeeded_task` are
**understatements**, and comparing two strategies with different pricing coverage is not
like-for-like.

**No aggregate is ever `nan` or `inf`.** All four guarded denominators return `0.0`:
`success_rate` and `mean_tool_calls_per_task` on `distinct_task_count`,
`mean_cost_per_succeeded_task` on `succeeded_count`, `cached_token_ratio` on `total_input_tokens`.
Every division routes through one `_safe_ratio()` helper that also rejects a non-finite result.
`test_zero_success_group_renders_without_nan` builds an all-failed group (three of the four
denominators zero at once) and asserts neither render contains `nan`, `NaN`, `Infinity`, or `inf`.
`render_json` additionally passes `allow_nan=False` as a backstop, so a non-finite value would raise
rather than emit a token.

**Field naming collision resolved as directed.** `MetricSummary` uses `event_count` and
`distinct_task_count`; there is no bare `task_count` on it. `BaselineReport.task_count` is
`len(corpus)`. `docs/evaluation.md` states why they differ (defined vs. observed).

**`succeeded_count` is task-scoped, not event-scoped** — distinct `task_id` values with at least one
`turn_succeeded: true` event. This keeps `success_rate` a genuine rate in `[0, 1]` and makes
`mean_cost_per_succeeded_task` design.md §15.2's "cost per successful task" rather than a per-event
average. Documented explicitly in both the `MetricSummary` docstring and `docs/evaluation.md`,
because the alternative reading is plausible enough to be assumed silently.

---

## Reproducibility: proven at three levels

**1. Two consecutive renders are byte-identical and contain no `nan`.**

```
$ PYTHONPATH=src python -c "... a=subprocess.run(c).stdout; b=subprocess.run(c).stdout; \
    assert a==b and a.strip(); assert 'nan' not in a and 'Infinity' not in a"
byte-identical, no nan/Infinity; 6473 chars
```

**2. Argument order does not reach the output.** Both formats:

```
$ ... --runs recorded-run-a.json --runs recorded-run-b.json --format json > r1.json
$ ... --runs recorded-run-b.json --runs recorded-run-a.json --format json > r2.json
$ cmp r1.json r2.json
reversed-argument renders are byte-identical
markdown reversed-argument byte-identical
```

**3. Working directory does not reach the output.** Rendered from the repo root and again from
`$TMP/deep/nested` using absolute input paths:

```
CLI render from a different cwd is byte-identical (6672 bytes)
"generated_from": [ "recorded-run-a.json", "recorded-run-b.json" ]
clean (no path separators in report)
```

`generated_from` holds basenames only — `scripts/benchmark.py` passes
`tuple(sorted(path.name for path in run_paths))`, and `build_report` re-sorts. The rendered report
contains no `/` or `\` at all.

Supporting mechanisms: entries are sorted by `(task_id, event_id)` before aggregation so neither file
order nor parsed-JSON insertion order can reach a float sum; aggregates are rounded to 6 dp; and
float fields render as fixed-precision six-digit **decimal strings**, never as float reprs. That last
choice is a deliberate deviation in spirit from emitting bare JSON numbers — it is the literal
reading of the plan's `f"{value:.6f}"` requirement and it makes a `NaN`/`Infinity` token
structurally impossible in the output. Integer fields stay integers.

---

## Files created (exactly the 9 in `files_modified`, nothing else)

| Path | Purpose |
|---|---|
| `src/hermes_auto/evaluation/corpus.py` | `BaselineTask`, `CorpusError`, `VALID_CATEGORIES`, `SUPPORTED_CORPUS_VERSION`, `load_corpus`. Validates version, structure, required keys, unknown keys, duplicate ids, and category membership; returns tasks sorted by `task_id`. `yaml.safe_load` only. |
| `src/hermes_auto/evaluation/metrics.py` | `MetricSummary` (20 fields across quality, economics, performance, cache, tool calls) and `aggregate()`. Nearest-rank percentiles, guarded divisions, 6-dp rounding, deterministic entry ordering. |
| `src/hermes_auto/evaluation/baselines.py` | `BASELINE_STRATEGIES` (all nine, design.md §15.1 order), `BaselineReport`, `build_report`, `render_markdown`, `render_json`. |
| `scripts/benchmark.py` | `argparse` CLI: `--corpus`, repeatable `--runs`, `--format {json,markdown}`, `--output`. Exit 0 / exit 2 with the message on stderr. No network, no env var, no credential. |
| `tests/fixtures/baseline/corpus.yaml` | 11-task corpus with a content-free-by-contract header comment. |
| `tests/fixtures/baseline/recorded-run-a.json` | 10 events, `strongest-only`, `test-model-a`. |
| `tests/fixtures/baseline/recorded-run-b.json` | 10 events, `user-fixed-model`, `test-model-b`. |
| `tests/performance/test_baseline_determinism.py` | 14 tests, all `pytest.mark.performance`, using the `repo_root` fixture from `tests/conftest.py` (read, never modified). |
| `docs/evaluation.md` | Methodology: why a baseline first, the corpus, the nine baselines, the metric mapping, the reproducibility contract, how to run it, and what is not yet measured. |

`scripts/` and `tests/fixtures/baseline/` were created by this plan, as the plan specified.
`git status --porcelain -uall` shows exactly these nine paths and nothing else.

### One addition beyond the plan's enumerated interface

`corpus.py` exports `SUPPORTED_CORPUS_VERSION = 1` in addition to the four names the plan named, so
`BaselineReport.corpus_version` has a single source of truth rather than a literal `1` duplicated in
`baselines.py`. No verification command constrains `__all__`; recorded here rather than left for a
reader to notice.

---

## Verification record

**Every `> verification:` line and every plan-level command was run with
`./.venv/Scripts/python.exe` through the Bash tool. 33 gates run, 33 exited 0, 0 failures,
0 fix attempts.**

| Gate group | Result |
|---|---|
| Task 1 in-task gates | 7/7 pass |
| Task 2 in-task gates | 10/10 pass |
| Task 3 in-task gates | 11/11 pass |
| Plan-level `verification_commands` | 9/9 pass |

(Task-3 and plan-level sets overlap on the pytest gate.)

```
$ ./.venv/Scripts/python.exe -m pytest tests/performance -q
14 passed in 0.47s
```

Evidence gathered beyond the required gates:

- **Whole-suite run, for information only** (the phase-close gate, not this plan's):
  `./.venv/Scripts/python.exe -m pytest tests/ -q` → **70 passed in 1.78s**. Nothing to escalate;
  the 56 tests from plans 01-03, 01-04, and 01-05 remain green alongside this plan's 14.
- **Corpus error paths exercised directly**, not merely claimed — wrong version, duplicate id, bad
  category, and missing key each raise `CorpusError` naming the offending id and value.
- **CLI failure path**: an unknown `task_id` exits **2** with
  `benchmark: recorded run for strategy 'strongest-only' references task_id 'does-not-exist', which
  is absent from the corpus; known task ids are [...]` on stderr.
- **Fixture privacy scan** across all three baseline fixtures (credential, key, and real-path
  patterns): clean.
- **`git diff --exit-code design.md`** → 0.
- **Forbidden-footprint check** over `src/hermes_auto/data`, `src/hermes_auto/gateway`,
  `tests/conftest.py`, `tests/contract`, `tests/unit`, `tests/performance/__init__.py`,
  `pyproject.toml`, `.github/`, `docs/adr`, `docs/architecture.md`, `docs/threat-model.md`,
  `docs/privacy.md`, `src/hermes_auto/routing`, `src/hermes_auto/compatibility.py`,
  `src/hermes_auto/telemetry` → clean.

Real fixture aggregate (run-a alone), for the record:

```
events=10 tasks=10 succeeded=9 success_rate=0.900 total_cost=0.0554 USD
known_cost=8 unknown_cost=2 ttft_p50=380ms cached_ratio=0.6453 tool_calls=64
```

---

## design.md §15.2 metrics not computable without live provider runs

Recorded honestly rather than silently omitted. None of these warranted a `BLOCKED`: the plan's own
stop gate covers "producing a metric would require invoking a model", and the plan simultaneously
directs that the harness make no such call — so the correct outcome is to name the gap and the phase
that closes it, which `docs/evaluation.md` § "What this does not measure yet" does in full.

**Blocked on a live provider or judge (Phase 8):** tests passed; human or judge preference; user
correction rate; accepted-edit rate; cost per accepted edit; cost per passing benchmark; cost per
commit. design.md §14.3's judge-bias controls (reversed answer order, repeated judgments, calibrated
human subsets, versioned judge prompts) are unbuilt.

**Blocked on a router that does not exist yet (Phase 4):** router-decision latency, and with it the
§15.4 "deterministic router p99 under 50 ms" gate; model-switch rate; cache savings lost to switches.
Route regret needs `oracle-hindsight`, which needs several strategies over one corpus.

**Blocked on a field absent from `outcome-event.v1`:** structured-output validity; tool-loop
duration; queue time; stream failures; sidecar availability. Adding any of these requires a `v2`
schema bump per plan 01-04's contract — not a `v1` loosening.

**Signal already present, aggregation not yet written** (cheapest to close, no contract work needed):
`retry_count` (repeated-attempt rate), `event_type: "fallback_used"` (fallback success rate),
`error_class: "context_overflow"` (context-limit errors).

---

## Handoff — reproducing a baseline report (Phase 8)

From the repository root, with `./.venv/Scripts/python.exe`:

```bash
# JSON report from one recorded run
PYTHONPATH=src python scripts/benchmark.py \
  --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-a.json \
  --format json

# Markdown report from two, to a file
PYTHONPATH=src python scripts/benchmark.py \
  --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-a.json \
  --runs tests/fixtures/baseline/recorded-run-b.json \
  --format markdown --output baseline-report.md

# Prove reproducibility: identical bytes despite reversed argument order
PYTHONPATH=src python scripts/benchmark.py --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-a.json \
  --runs tests/fixtures/baseline/recorded-run-b.json --format json > r1.json
PYTHONPATH=src python scripts/benchmark.py --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-b.json \
  --runs tests/fixtures/baseline/recorded-run-a.json --format json > r2.json
cmp r1.json r2.json

# Confirm every recorded event conforms to the frozen schema
PYTHONPATH=src python -c "import json; from hermes_auto.gateway.schemas import validate; \
  d=json.load(open('tests/fixtures/baseline/recorded-run-a.json')); \
  [validate(e['event'], 'https://hermes-auto-router.dev/schema/routing/outcome-event.v1.json') \
   for e in d['events']]"

# The determinism suite
python -m pytest tests/performance -q
```

### Notes for whoever records the first real runs

1. **Write the wrapper, not a flat event.** Each `events` entry is
   `{"task_id": ..., "event": {...}}`. Validate `entry["event"]`. A `task_id` inside the event can
   never validate.
2. **Emit `null`, never `0`, for an unpriced candidate.** Emitting `0` would make an unknown-cost
   local model look measurably free and would corrupt the §15.4 20%-cost-reduction gate in the
   direction of a false pass.
3. **Load the schema mapping once** and pass it as `validate()`'s third argument in any loop —
   `load_schemas()` does not cache and re-reads every file otherwise. `validate()` raises `KeyError`
   for an unknown `$id` and `jsonschema.ValidationError` for a bad instance; catch them separately.
4. **`format: "date-time"` is advisory.** Per 01-04's handoff, `jsonschema` does not enforce it
   without a `FormatChecker`. A malformed `occurred_at` will pass validation. The harness never
   parses `occurred_at`, so this cannot affect a report today — but a Phase 8 store that orders by it
   should validate it.
5. **Never add a provenance field to `BaselineReport`.** `test_report_contains_no_volatile_fields`
   blocks `generated_at`, `timestamp`, `hostname`, `platform`, and `python_version` by test.
   Provenance goes in a summary artifact like this one.
6. **`task_id` values are published.** Renaming or reusing one silently invalidates every stored
   comparison against it. Add new tasks with new ids.
7. **Recording new strategies needs no code change.** `build_report` groups by the run's `strategy`
   field against `BASELINE_STRATEGIES`; the seven currently-empty strategies already render as zero
   rows, so a Phase 4 or Phase 9 report is a numeric diff against a Phase 1 one, not a structural
   one.

---

## Issues

1. **`build_report` takes no corpus-version argument.** `BaselineReport.corpus_version` is
   `SUPPORTED_CORPUS_VERSION`, the version `load_corpus` already enforced. If a `version: 2` corpus
   format is ever introduced, `build_report` will need the version threaded through from the loader
   rather than read from a constant. Noted now because the constant is correct today and would
   silently stay `1` otherwise.
2. **Float fields render as decimal strings, not JSON numbers.** A deliberate determinism choice
   (see above), but it means a JSON consumer must cast. Documented in `docs/evaluation.md`.
   Integer fields are unaffected.
3. **Seven of the nine §15.1 baselines have no runs and cannot have any in Phase 1.** Not a defect —
   the report shape is deliberately stable across phases — but a reader of a Phase 1 report should
   not mistake seven zero rows for seven measured zeros. Both renders carry a line saying so.

## Errors

None.
