# Evaluation methodology

How the Hermes Auto Router's numbers are produced, and what they are allowed to
claim. The source of truth is [`design.md`](../design.md) §15 (Evaluation
program); §15.1 fixes the baselines, §15.2 the metrics, §15.3 the corpus
categories, and §15.4 the release gates. For where this sits in the system, see
[`docs/architecture.md`](architecture.md).

The harness delivered in Phase 1 lives in `src/hermes_auto/evaluation/` and
`scripts/benchmark.py`. It is an **offline aggregator over recorded runs**. It
makes no network call, invokes no provider, and reads no credential. Producing
recordings against real providers is Phase 8 work.

---

## Why a baseline first

design.md §14.1 opens on an uncomfortable finding: under unified evaluation,
many sophisticated routers do **not** reliably outperform simple baselines, and
careful curation of the candidate pool is often worth more than enlarging it.

The consequence for this project is procedural, not philosophical. Every claim
the router will eventually make — the §15.4 "at least 20% cost reduction in
`balanced`" gate, the quality non-inferiority gate, Phase 8's router-benefit
measurement — is a claim *relative to a baseline*. If no measured baseline
exists, those claims are unfalsifiable; if the baseline is not reproducible,
they are unfalsifiable in a way that looks like data. So the baseline is built
before any routing logic exists, and its exit criterion is reproducibility
rather than any particular number.

That ordering is why this document exists in a phase that deliberately contains
no router.

---

## The corpus

`tests/fixtures/baseline/corpus.yaml`, loaded by
`hermes_auto.evaluation.corpus.load_corpus`.

The corpus spans the nine workload categories of the design.md §15.3 dynamic
execution track, with at least one task each:

| Category | What it exercises |
|---|---|
| `bug_fixing` | Localizing and repairing a defect surfaced by a failing test |
| `feature_implementation` | Adding new behavior with covering tests |
| `test_repair` | Restoring a suite broken by drift, without weakening assertions |
| `refactoring` | Structural change with no behavior change |
| `multi_step_shell` | A sequence where each step depends on the previous exit status |
| `web_research` | Surveying external documentation to inform one decision |
| `data_extraction` | Producing schema-valid structured output from loose input |
| `vision_assisted` | Work that requires reading a supplied image |
| `context_compression` | Continuing after the context window is exceeded |

Three rules govern the file:

**Descriptions are content-free abstractions.** A `description` names the
*shape* of the work, never its content. "Fix a null-dereference crash surfaced
by a failing unit test" is correct; pasting the traceback is not. No prompt
text, code, diff, log excerpt, real repository path, hostname, or credential
belongs in a corpus file. This is the same prohibition
[`docs/privacy.md`](privacy.md) makes for the telemetry store, applied to
evaluation data — a corpus is not exempt because it is "only test data".

**`task_id` is stable forever.** It is the join key between the corpus and every
recorded run. Once a baseline has been recorded against an id, renaming or
reusing that id silently invalidates every stored comparison that referenced it.
Add new tasks with new ids instead. `load_corpus` rejects duplicates by name.

**A majority of tasks are mechanically verifiable.** `verifiable: true` means
success is checkable by a test outcome, a process exit code, or an observable
file state, with no judge in the loop. design.md §14.3 prefers execution-derived
evidence precisely because judge-scored quality is the noisiest input to any
routing claim, so the corpus is weighted toward the mechanical end. The
non-verifiable tasks (research, vision, context compression) are kept because
excluding them would bias the corpus toward exactly the work a cheap model finds
easiest.

`load_corpus` returns tasks **sorted by `task_id`**. Report determinism starts
there: reordering the YAML file cannot change the rendered output.

---

## The nine baselines

`BASELINE_STRATEGIES` in `hermes_auto.evaluation.baselines`, in design.md §15.1
order.

| Strategy | Meaning | Recordable in Phase 1? |
|---|---|---|
| `cheapest-only` | Always the cheapest eligible candidate | Yes — a fixed-model run |
| `strongest-only` | Always the strongest candidate | Yes — a fixed-model run |
| `user-fixed-model` | Whatever model the user has configured today | Yes — this is the incumbent to beat |
| `random-eligible` | Uniform choice among eligible candidates | Yes, once eligibility exists (Phase 3) |
| `rule-based-tiers` | Hand-written difficulty tiers | No — needs the tier rules (Phase 4) |
| `deterministic-capability-router` | Capability matching, no learning | No — Phase 4 |
| `capability-plus-domain-affinity` | Capability matching plus domain tags | No — Phase 4 |
| `capability-plus-learned-residual` | Adds the learned outcome residual | No — Phase 10 |
| `oracle-hindsight` | Best possible choice known after the fact | No — computable only once several strategies have run over the same corpus |

Only the fixed-model baselines can be recorded now, because the machinery that
would produce the other rows does not exist yet. **A strategy with no recorded
runs is reported as a zero row rather than omitted.** That keeps the report
shape constant across phases, so a Phase 8 report differs from a Phase 1 report
only in its numbers — a diffable comparison rather than a structural one — and a
strategy that is missing cannot be mistaken for a strategy that scored zero.

---

## Metrics

Recorded runs are aggregated by `hermes_auto.evaluation.metrics.aggregate` into a
`MetricSummary`. Each entry of a run's `events` array is exactly
`{"task_id": "<corpus id>", "event": {…}}`; the inner object validates against
`outcome-event.v1.schema.json`, which sets `additionalProperties: false` and
defines no `task_id`, so the join key lives beside the event rather than inside
it.

The five dimensions design.md §18 Phase 0 item 5 requires, and the field that
reports each:

| Dimension | design.md §15.2 metric | `MetricSummary` field |
|---|---|---|
| Quality | Events aggregated | `event_count` |
| Quality | Distinct tasks observed | `distinct_task_count` |
| Quality | Task success | `succeeded_count`, `success_rate` |
| Quality | Invalid tool calls | `invalid_tool_call_count` |
| Quality | Empty responses | `empty_response_count` |
| Economics | Realized spend | `total_cost_usd` (known values only) |
| Economics | Pricing coverage | `known_cost_count`, `unknown_cost_count` |
| Economics | Cost per successful task | `mean_cost_per_succeeded_task` |
| Performance | Time to first token | `ttft_ms_p50`, `ttft_ms_p95` |
| Performance | End-to-end latency | `total_latency_ms_p50`, `total_latency_ms_p95` |
| Cache | Cached-token ratio | `total_input_tokens`, `total_cached_tokens`, `cached_token_ratio` |
| Tool calls | Tool-call volume | `total_tool_calls`, `mean_tool_calls_per_task` |

Definitions worth stating explicitly, because they are easy to assume wrongly:

- `succeeded_count` counts **distinct tasks** with at least one event reporting
  `turn_succeeded: true`, not events. `success_rate` is therefore a genuine rate
  in `[0, 1]` over tasks, and `mean_cost_per_succeeded_task` is design.md
  §15.2's "cost per successful task" rather than a per-event average.
- `distinct_task_count` is what the harness *observed*; `BaselineReport.task_count`
  is what the corpus *defines*. They differ whenever coverage is partial, which
  is why they are deliberately not the same name.
- Percentiles are **nearest-rank**, so they always return an observed value and
  need no tie-breaking rule. A single sample returns that sample.

### The unknown-cost rule

`actual_cost_usd` is nullable, and null means *unknown* — an unpriced local
candidate, or a provider that reported no figure. Unknown is never reconciled as
zero (design.md §7.5). In the harness this means: **an unpriced candidate is
counted, never valued at zero**. Only known values are summed into
`total_cost_usd`; everything else increments `unknown_cost_count`. An event with
the key absent entirely is treated as unknown too, because a missing measurement
is not evidence of a free turn.

The practical consequence, which any consumer of a baseline report must carry:
whenever `unknown_cost_count` is non-zero, `total_cost_usd` and
`mean_cost_per_succeeded_task` are **understatements**, and a cost comparison
between two strategies with different pricing coverage is not a like-for-like
comparison. Report the count alongside the total, always.

### No metric is ever `nan`

Every ratio and mean divides by a denominator that can legitimately be zero, and
each returns `0.0` in that case:

| Field | Denominator | Value when the denominator is 0 |
|---|---|---|
| `success_rate` | `distinct_task_count` | `0.0` |
| `mean_tool_calls_per_task` | `distinct_task_count` | `0.0` |
| `mean_cost_per_succeeded_task` | `succeeded_count` | `0.0` |
| `cached_token_ratio` | `total_input_tokens` | `0.0` |

These are live code paths, not theoretical ones: a group in which every turn
failed has zero successes. `f"{float('nan'):.6f}"` renders the literal text
`nan`, which would render, print, and parse without complaint while silently
defeating every byte-comparison downstream —
`test_zero_success_group_renders_without_nan` exists to stop exactly that.

---

## Reproducibility contract

design.md §18 Phase 0's exit criterion is "baseline tasks run reproducibly."
Five properties define what that means here, each enforced by a test in
`tests/performance/test_baseline_determinism.py`:

1. **Identical inputs produce byte-identical output.** Two renders of the same
   corpus and the same recorded runs are equal string-for-string.
2. **Argument order does not affect output.** `--runs a --runs b` and
   `--runs b --runs a` render identically. Inputs are sorted at the CLI boundary
   and events are re-sorted by `(task_id, event_id)` before aggregation, so
   neither file order nor parsed-JSON insertion order can reach a float sum.
3. **The working directory does not affect output.** `generated_from` holds
   **basenames only**, never absolute or relative paths. A path there would make
   the rendered bytes depend on where the harness happened to be invoked.
4. **The report carries no volatile field.** No timestamp, hostname, platform,
   interpreter version, package version, or random value appears anywhere in a
   rendered report. Provenance belongs in the surrounding summary artifact, not
   in a document whose whole purpose is to be byte-compared. This is asserted by
   test rather than left to convention.
5. **Floats are fixed-precision, and every zero denominator resolves to `0.0`.**
   Aggregates are rounded to six decimal places, and rendering formats them as
   six-digit decimal strings rather than using `str()` or a float repr, so no
   `NaN` or `Infinity` token can reach the output at all.

**A change that breaks any of these invalidates every prior baseline
comparison.** Stored reports from earlier phases stop being comparable to new
ones, and there is no way to tell from the report itself that this happened —
which is precisely why the properties are pinned by test.

---

## Running it

The corpus loader and metric aggregation, direct:

```bash
PYTHONPATH=src python -c "from hermes_auto.evaluation.corpus import load_corpus; print(len(load_corpus('tests/fixtures/baseline/corpus.yaml')))"
```

A JSON report from one recorded run:

```bash
PYTHONPATH=src python scripts/benchmark.py \
  --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-a.json \
  --format json
```

A Markdown report from two, written to a file:

```bash
PYTHONPATH=src python scripts/benchmark.py \
  --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-a.json \
  --runs tests/fixtures/baseline/recorded-run-b.json \
  --format markdown \
  --output baseline-report.md
```

Proving reproducibility — the two renders must be identical despite the reversed
argument order:

```bash
PYTHONPATH=src python scripts/benchmark.py --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-a.json \
  --runs tests/fixtures/baseline/recorded-run-b.json --format json > r1.json
PYTHONPATH=src python scripts/benchmark.py --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-b.json \
  --runs tests/fixtures/baseline/recorded-run-a.json --format json > r2.json
cmp r1.json r2.json
```

Confirming every recorded event conforms to the frozen outcome-event schema:

```bash
PYTHONPATH=src python -c "import json; from hermes_auto.gateway.schemas import validate; d=json.load(open('tests/fixtures/baseline/recorded-run-a.json')); [validate(e['event'], 'https://hermes-auto-router.dev/schema/routing/outcome-event.v1.json') for e in d['events']]"
```

The determinism suite:

```bash
python -m pytest tests/performance -q
```

`scripts/benchmark.py` exits `0` on success and `2` on a corpus or recorded-run
error, with the message on stderr.

---

## What this does not measure yet

Stated plainly, because a baseline document that overclaims is worse than none.

**No live provider runs.** The fixtures in `tests/fixtures/baseline/` are
synthetic. They exercise the aggregation logic — including a null cost, a failed
turn, invalid tool calls, and both cached and uncached events — but they are not
measurements of any model. Real recordings arrive in **Phase 8**.

**No judge-scored quality.** design.md §15.2 lists human or judge preference,
accepted-edit rate, and user correction rate. All three need either a live
harness or an LLM judge, and design.md §14.3's bias controls (reversed answer
order, repeated judgments, calibrated human subsets, versioned judge prompts)
are unbuilt. **Phase 8.**

**No router-decision latency.** design.md §15.2's "router-decision latency" and
the §15.4 "deterministic router p99 under 50 ms" gate cannot be measured because
no router exists to time. **Phase 4** delivers the decision path; these tests
measure the harness's determinism, not the system's speed.

**No route regret and no oracle.** Regret is defined against
`oracle-hindsight`, which requires several strategies to have run over the same
corpus. Not computable from fixed-model runs alone.

Individual design.md §15.2 metrics not computable in Phase 1, with the reason:

| Metric | Why not yet |
|---|---|
| Tests passed | Requires the dynamic execution track actually running tasks (Phase 8) |
| Human or judge preference | Requires a judge (Phase 8) |
| Structured-output validity | No field in `outcome-event.v1`; add in a `v2` bump |
| User correction rate, accepted-edit rate | Phase 8 opt-in code outcomes |
| Repeated-attempt rate | `retry_count` is recorded per event but not yet aggregated |
| Route regret against oracle | Needs `oracle-hindsight` runs |
| Cost per accepted edit / passing benchmark / commit | Phase 8 opt-in code outcomes |
| Cache savings lost to switches | Needs switch detection from decision sequences (Phase 4+) |
| Router-decision latency | No router (Phase 4) |
| Tool-loop duration, queue time | No field in `outcome-event.v1` |
| Model-switch rate | Needs the decision sequence, not isolated events (Phase 4/8) |
| Stream failures, sidecar availability | No field in `outcome-event.v1`; gateway behavior (Phase 2+) |
| Fallback success rate | `event_type: "fallback_used"` exists but is not yet aggregated |
| Context-limit errors | `error_class: "context_overflow"` exists but is not yet aggregated |

The last four are the cheapest to add: the schema already carries the signal, so
they are aggregation work rather than contract work.
