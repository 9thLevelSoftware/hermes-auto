# Phase 1: Contracts, Schemas & Baselines — Review Summary

## Result: PASSED

**Cycles used**: 3 of 3
**Reviewers**: testing-qa-verification-specialist, testing-api-tester, engineering-security-engineer, product-technical-writer (dynamic panel, non-overlapping domain rubrics)
**Completed**: 2026-07-27

## Findings summary

| Metric | Cycle 1 | Cycle 2 | Cycle 3 |
|--------|---------|---------|---------|
| Blockers found | 3 | 3 | 0 |
| Warnings found | 17 | 12 | 0 |
| Suggestions | 6 | 5 | — |
| Blockers resolved | 3 | 3 | — |
| Tests | 70 | 140 | 206 |

**Total: 6 blockers and 29 warnings found and resolved across three cycles.** No finding was left unresolved. Two suggestions were consciously accepted rather than fixed (recorded below).

## The finding that mattered most

Three cycles of security review converged on a single root cause, and it was not under-constraining. It was **overclaiming**.

Cycle 1 found that `additionalProperties: false` stops a contributor adding a field *named* `prompt_text` but does nothing about overloading an existing identifier field — a raw prompt with an embedded API key validated in `root_session_hash`, `lane_id`, `event_id`, and `candidate`. Six of seven smuggling payloads succeeded.

Cycle 2 tightened those fields, then ran 26 payloads. All 7 originals were dead. **17 new ones got through**: content decimal-encoded into `occurred_at`'s unbounded fractional-second digits, prompts chunked across a 200-char-capped array with no `maxItems`, credentials in identifier fields, arbitrary text in two unbounded version strings.

The reviewer's conclusion, which the phase adopted:

> `additionalProperties: false` plus value constraints bound the **shape** of what can be written; they do not bound **intent**. Phase 1 has no write path, so nothing here is exploitable today — but every absolute claim of the form "cannot carry X" is a claim about Phase 8 code that has not been written.

A 40-character GitHub token is indistinguishable from a legitimate identifier. No character class fixes that. Cycle 3 therefore took the cheap structural bounds *and* deleted every absolute claim, replacing each with a precise statement of what is bounded plus explicit scoping to the Phase 8 write path. A test now walks every schema description at every depth against 11 banned phrasings — it caught one of the replacement descriptions mid-authoring.

Ten vectors remain open **by design**, asserted as open in a green test so no future description can claim otherwise. That is the honest end state: a field that must carry a real value must accept the bytes real values are made of.

## Findings detail

### Cycle 1 — 3 blockers, 17 warnings

| Sev | File | Issue | Fixed |
|-----|------|-------|-------|
| BLOCKER | `outcome-event.v1` | Unbounded identifier strings accepted raw prompts and credentials; description made a false absolute claim | c1 |
| BLOCKER | `privacy.md`, `adr/0004` | "Cross-machine correlation unavailable by construction" had zero enforcement | c1 (reopened c2) |
| BLOCKER | `privacy.md` | Worked example validated against neither schema while adjacent prose cited the schema as proof | c1 |
| WARNING | `metrics.py` | Float and negative counts silently became 0, making `cached_token_ratio` read 0.000000 | c1 |
| WARNING | `benchmark.py` | Malformed run files tracebacked instead of the documented exit 2; zero test coverage | c1 |
| WARNING | `ci.yml` | Wheel gate existed only as markdown; the editable install provably could not detect a `package-data` regression | c1 |
| WARNING | `hermes-compat.yml` | Probe asserted only `not VERSION_UNREADABLE` — passed on 3 of 4 statuses including "Hermes absent" | c1 |
| WARNING | wire schemas | `contentPart` was the one closed union in a relay schema, rejecting audio, `cache_control`, unknown parts | c1 |
| WARNING | `schemas.py` | `format` keywords inert; no `$ref` registry so the `_hermes_auto` protocol-version guard was unenforced | c1 |
| WARNING | `route-decision.v1` | `reason_codes: []` and duplicates validated | c1 |
| WARNING | `threat-model.md` | Row 6 attributed CI dependency scanning to "Phase 1" — no lockfile or scanner exists | c1 |
| WARNING | `threat-model.md` | Admin-API boundary declared but no row owns it | c1 |
| WARNING | docs ×4 | Stale forward-reference labels, Phase 9/10 misassignment, unscoped present-tense gateway claim, changelog recording 1 of 7 plans | c1 |

### Cycle 2 — 3 blockers, 12 warnings

| Sev | File | Issue | Fixed |
|-----|------|-------|-------|
| BLOCKER | `privacy.md`, `adr/0004` | The replacement claim was still false: `^[0-9a-f]{64}$` enforces "looks like SHA-256", orthogonal to "salted". An unsalted digest validates identically and is cross-machine stable | c3 |
| BLOCKER | `model-card.v1` | `id` left unbounded while every consumer was tightened — a card with `id: "openai/gpt-4o"` validated but its decision record did not | c3 |
| BLOCKER | `route-decision.v1` | Two fully unbounded strings (`model_card_version`, `router_version`) while the description claimed one bounded free-text channel | c3 |
| MAJOR | `benchmark.py` | `json.load` accepts `NaN`/`Infinity`; `nan < 0` is `False` so `minimum: 0` did not reject. A `NaN` cost reached the report at exit 0, falsifying a bolded plan constraint | c3 |
| MAJOR | `schemas.py` | `validate()` rebuilt a validator per call — 49× overhead, which is what `--no-validate` existed to work around | c3 |
| WARNING | `outcome-event.v1` | `occurred_at` unbounded fractional digits; pattern shape-only so `2026-13-45T99:99:99Z` validated while RFC-3339-legal lowercase `t`/`z` did not | c3 |
| WARNING | `route-decision.v1` | No `maxItems` on `reasons`, `excluded`, `ranked`; unbounded integers | c3 |
| WARNING | `outcome-event.v1` | `error_class` had a length cap but no character class | c3 |
| WARNING | `schemas.py` | `$ref` resolved lazily, so a wire-scoped load passed its own tests and failed only on real traffic carrying `_hermes_auto` | c3 |
| WARNING | `pyproject.toml` | `referencing` imported directly but undeclared, resolving transitively | c3 |
| WARNING | both routing schemas | `decision_id` rejected `str(uuid4())`, the obvious generator | c3 |
| WARNING | `evaluation.md` | Never documented the validation pass or `--no-validate` shipped in the same commit; two of three `metrics.py` invariants documented | c3 |

### Cycle 3 — 0 findings

Verified independently by the coordinator: 206 tests pass, all five candidate-id sites carry a byte-identical pattern that accepts `openai/gpt-4o`, `NaN` exits 2 on both paths, reports remain byte-identical under reversed argument order, `design.md` matches its pinned blob, and a built wheel carries all seven schemas.

## Reviewer verdicts

| Reviewer | Cycle 1 | Cycle 2 | Key contribution |
|---|---|---|---|
| testing-qa-verification-specialist | With fixes | With fixes | Proved the CI wheel gate could not detect a `package-data` regression by injecting one; found the `NaN` hole; confirmed JSON Schema `integer` accepts `4200.0` |
| testing-api-tester | With fixes | With fixes | Found the `model-card.id` contract break; verified the `contentPart` rewrite did not silently stop constraining (11 of 13 malformed parts still rejected) |
| engineering-security-engineer | With fixes | **No** | Ran 26 smuggling payloads across two cycles; identified the shape-vs-intent root cause that reframed the whole class |
| product-technical-writer | With fixes | With fixes | Verified 28 `design.md §N` citations across five ADRs — all correct; caught the fabricated privacy example |

## What held up under review

Worth recording, because it is the part that needed no fixing:

- **All 28 `design.md §N` citations spot-checked correct** across five ADRs and both overview documents, against explicit expectation of error — two off-by-a-section mistakes had already been found during planning
- **All 14 §21 threat rows present, faithful, and verbatim**, each with an owning module path and implementing phase
- **All five ADRs structurally complete** with substantive negative consequences and alternatives carrying rejection reasons
- **Frozen vocabularies byte-intact** through three cycles of schema edits: eight capability dimensions, ten domain tags, twelve reason codes, `transport.required` exactly three, `economics` nullable with no `default`
- **Zero committed secrets**; `credential_ref` resisted eight smuggling variants
- **Exit criterion 6 verified by symbol grep** — no routing, scoring, eligibility, or candidate-selection code exists

## Suggestions accepted, not fixed

- **Plan 01-06's guard uses `git diff --quiet`**, which cannot detect an untracked forbidden file. The plan is an executed historical artifact and the file it guarded is now tracked, so the defect is moot. The lesson belongs in Phase 2 plan authoring, and is recorded in STATE.
- **Absolute paths containing the developer's OS username** appear in `.planning/` summaries. Username disclosure only, no secret. Worth a sweep before the repository goes public; recorded in STATE.

## Post-review polish

Skipped — `settings.review.polish` is unset and no `settings.json` exists, so the default applies, but three cycles of fix agents have already touched every file in the phase. A fourth pass over freshly-reviewed code carries more regression risk than clarity benefit.
