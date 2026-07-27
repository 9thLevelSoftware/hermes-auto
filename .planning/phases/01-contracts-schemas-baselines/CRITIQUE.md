# Plan Critique — Phase 1: Contracts, Schemas & Baselines

**Date**: 2026-07-26
**Verdict**: **REWORK**
**Rule fired**: Section 3 Step 2 — "REWORK: … OR any High impact decision-completeness gap OR 3+ critical items". Four High-impact decision-completeness gaps (G1–G4), four critical pre-mortem risks, and six critical assumptions.

**Agents**: `testing-qa-verification-specialist` (pre-mortem, Section 1 + completeness Section 2.5), `product-sprint-prioritizer` (assumption hunting, Section 2 + decision-completeness Section 2.6). Both read-only.

| Metric | Count |
|--------|-------|
| Pre-mortem failure scenarios | 8 (6 live, 2 cleared) |
| Critical risks (score ≥ 6) | 4 |
| Watch items | 4 |
| Assumptions extracted | 15 |
| Critical assumptions | 6 |
| Warning assumptions | 5 |
| Decision-completeness gaps | 9 (4 High, 4 Medium, 1 Low) |
| Completeness score | 40% (8 of 20 tasks gap-free) |
| Merged findings | 14 |

## Schema Conformance (Section 5) — all PASS

| Plan | verification_commands | files_forbidden | expected_artifacts | Wave overlap | Status |
|------|----------------------|-----------------|--------------------|--------------|--------|
| 01-01 | PASS (6) | PASS | PASS | — | PASS |
| 01-02 | PASS (6) | PASS | PASS | none w/ 01-01 | PASS |
| 01-03 | PASS (6) | PASS | PASS | none in wave 2 | PASS |
| 01-04 | PASS (6) | PASS | PASS | none in wave 2 | PASS |
| 01-05 | PASS (7) | PASS | PASS | none in wave 2 | PASS |
| 01-06 | PASS (7) | PASS | PASS | none in wave 2 | PASS |
| 01-07 | PASS (7) | PASS | PASS | — (sole wave 3) | PASS |

Section 6 wave file-overlap detection: no overlaps in any wave. File ownership across the parallel wave-2 plans is clean.

One defect was found and fixed during the mechanical pass, before the agents ran: plan 01-01 Task 3 created seven files (`tests/{unit,property,contract,integration,e2e,fault,performance}/__init__.py` and `tests/fixtures/.gitkeep`) that were absent from its `files_modified`, which would have tripped its own stop gate on correct work. `files_modified` was extended.

## Environment findings — empirically verified

Verified directly, not inferred:

| Fact | Value | Consequence |
|------|-------|-------------|
| Hermes distribution name | `hermes-agent` (`hermes` is ABSENT) | 01-05 hardcodes `"hermes"` — probe returns `None` forever |
| Installed Hermes version | `0.19.0` | 01-05's invented `>=0.1,<1.0` contains it only by luck |
| `sys.prefix` | `C:\Users\dasbl\AppData\Local\hermes\hermes-agent\venv` | active interpreter IS the Hermes venv |
| `pytest` | ABSENT | 01-01's `pip install -e ".[dev]"` WILL fire, into the Hermes venv |
| Python / tomllib | 3.11.15, present | 01-01's tomllib stop gate will not fire — CLEARED |
| jsonschema / pyyaml / packaging | 4.26.0 / 6.0.3 / 26.0 | already satisfied |
| bash | GNU bash 5.3.15 | POSIX verification commands run correctly — CLEARED |
| `test ! -f X \|\| exit 0` | exits 0 unconditionally | the cross-plan write guard in 01-02, 01-03, 01-07 verifies nothing |

## Merged findings, ranked

### F1 — Wrong Hermes distribution name and an invented version range (CRITICAL)
Plans: 01-05 Task 1, Task 3. Sources: risk 4, C1, C2, C8, G1.
`HERMES_DISTRIBUTION_NAME = "hermes"` is wrong — the installed distribution is `hermes-agent` 0.19.0. `SUPPORTED_HERMES = ">=0.1,<1.0"` has no basis in design.md, PROJECT.md, or anywhere else; it was invented by the planner (me). design.md §10.2 says only "explicit supported-version range"; §18 Phase 0 item 3 says only "pin supported Hermes versions". The task also contradicts its own stop gate: the action orders the hardcode while the gate says BLOCK if the name is not `hermes` — and no step tells the executor to check.
Compounding: 01-05 Task 3's `latest-release` job runs `pip install hermes` and is explicitly forbidden from `continue-on-error`, so the nightly either measures an unrelated PyPI package or goes permanently red while the ROADMAP criterion appears green.
**Fix**: set `HERMES_DISTRIBUTION_NAME = "hermes-agent"`; derive `SUPPORTED_HERMES` from the installed baseline (`>=0.19,<1.0`) or ask the user for the real range; delete the name-guessing stop gate since the name is now resolved at plan time; gate the nightly install on a provisioned variable.

### F2 — Verification scope exceeds file-ownership scope (CRITICAL, largest root cause)
Plans: 01-03 Tasks 2–3, 01-04 Tasks 2–3, 01-07 Task 3. Sources: risk 1, C5, C6, C12, W1, assumption C6.
01-04's gate `assert len(load_schemas()) >= 7` is not additive — four of those seven `$id`s belong to 01-03, making it a disguised mid-wave dependency on a plan 01-04 deliberately does not declare in `depends_on` and lists in `files_forbidden`. Symmetrically, 01-03 Task 2 calls `load_schemas()` over the shared root, where the loader is specified to *raise* on any unparseable or duplicate-`$id` file anywhere beneath it — including 01-04's half-written `routing/*.json`. Both plans then run `pytest tests/contract/` (the whole directory), collecting a test file each lists as forbidden and therefore cannot repair. 01-07 repeats it at wave scale with `pytest tests/ -q`.
**Fix**: scope every gate to owned files — 01-04 asserts `>=4` routing-prefixed ids and runs only its own test file; 01-03 loads `SCHEMA_ROOT/"wire"` and runs only its two test files; 01-07's own gate is `pytest tests/performance -q`. Move the `>=7` and full-suite assertions to a phase-close gate in `01-CONTEXT.md`.
This one fix resolves five of the twelve completeness gaps and raises the completeness score from 40% to roughly 65%.

### F3 — The `task_id` conditional is guaranteed to fire (CRITICAL)
Plans: 01-07 Task 2 against 01-04 Task 2. Sources: risk 2, assumption C5, G2.
01-07 Task 2's primary instruction is "add a `task_id` property to each event object", with the wrapper shape offered only as a fallback "if `additionalProperties: false` rejects it". It always rejects it — 01-04 sets `additionalProperties: false` at every level and defines no `task_id`. So the conditional is a coin-flip the executor must resolve, and the answer silently propagates into `metrics.py`, `build_report`, and three inline test dicts that nothing pins to the same shape.
Also: 01-07's `key_links` asserts fixtures "conform to the outcome-event schema" while no verification command validates a single fixture against it.
**Fix**: delete the conditional; mandate `{"task_id": …, "event": {…}}`; add a verification command that actually validates every fixture event through `validate()`.

### F4 — Execution would install into the Hermes Agent venv (CRITICAL)
Plans: 01-01 Task 3 and `<execution_contract>`. Sources: assumption C3, G4.
`pytest` is absent, so 01-01's `pip install -e ".[dev]"` path will execute; `sys.prefix` is the Hermes Agent venv and site-packages is writable, so it will succeed silently — violating PROJECT.md's own constraint "sidecar dependencies must be isolated to avoid conflicts with the Hermes environment". Phase 1 would ship a violation of the constraint Phase 1 documents. No plan has a `user_setup` entry.
**Fix**: add `user_setup` requiring a dedicated venv; create `.venv` and use `.venv/Scripts/python.exe` throughout; add a stop gate on `sys.prefix` containing `hermes-agent`.

### F5 — Byte-identical output is unachievable as specified (CRITICAL)
Plan: 01-07 Task 2. Sources: risk 3, G6, G8.
`BaselineReport.generated_from` is described as "the sorted input filenames" but the only specified constructor, `build_report(corpus, runs)`, receives parsed dicts and no filenames — the field cannot be populated without the executor inventing a path, and embedding argv paths makes output cwd-dependent. Nothing forbids a timestamp or hostname field. Separately `mean_cost_per_succeeded_task` and `cached_token_ratio` have undefined denominators when `succeeded_count == 0` or `total_input_tokens == 0`; zero-fill is defined only for an empty `runs` list, while the fixtures are *required* to contain a `turn_succeeded: false` event — so `nan` is reachable, and `f"{v:.6f}"` renders `nan`, defeating comparison. `MetricSummary.task_count` and `BaselineReport.task_count` also collide with different definitions.
**Fix**: add a `sources` parameter carrying basenames only; forbid timestamp/hostname/platform fields; define every zero-denominator case as `0.0` and forbid `nan`/`inf`; rename to `event_count` / `distinct_task_count`.

### F6 — Three verification commands verify nothing (HIGH)
Plans: 01-02 cmd 6, 01-03 cmd 6, 01-07 (analogous), 01-06 cmd 2, 01-05 cmd 7. Source: W3, C10.
`test ! -f docs/threat-model.md || exit 0` exits 0 unconditionally — confirmed empirically. `grep -c '^| ' docs/threat-model.md | xargs test 14 -le` counts every markdown table row including the STRIDE table and separators, so it passes with roughly six threat rows instead of fourteen. `01-05` cmd 7 raises `KeyError: True` when `on:` is written quoted.
**Fix**: replace the write guards with `git status --porcelain -- <path> | grep -q . && exit 1 || exit 0`; anchor the threat count to the table's STRIDE column signature; use the safer `w.get('on', w.get(True))` form.

### F7 — CI job content largely unverified, plus an incoherent instruction (HIGH)
Plan: 01-05 Task 3. Sources: C9, G3.
The `schema-validation` job, `permissions`, and `concurrency` blocks are all unverified — Task 3 can ship a workflow missing the entire second job and pass all seven gates. The instruction "write the command so it is tolerant of the schema directory being empty *only* if `load_schemas` itself is missing" is unexecutable prose that hands a design choice to the executor.
**Fix**: replace with one unconditional assertion step; add YAML gates asserting the job, permissions, and concurrency keys exist.

### F8 — `design.md` §12's own example contains a code absent from the frozen enum (MEDIUM, sharp catch)
Plan: 01-04 Task 3. Source: C7.
The fixture instruction says "mirrors the design.md §12 example structure", and that example emits `DEBUGGING_AFFINITY` — which is deliberately *not* among the twelve frozen codes (the code is `DOMAIN_AFFINITY`). A literal copy fails `test_route_decision_fixture_validates` against a *correct* schema, and an executor debugging it may widen the enum to thirteen, breaking the frozen §12 contract.
**Fix**: warn explicitly in the fixture instruction not to copy it and not to widen the enum.

### F9 — Plans are not pinned to a `design.md` revision (MEDIUM)
Source: W4. `design.md` is committed at `331a69d` and guarded by `git diff --exit-code` in all seven plans, but `.planning/` is entirely untracked — no commit pairs this plan set to a design.md revision, and every plan cites by section number. One inserted section renumbers all citations silently.
**Fix**: commit `.planning/` and record `git rev-parse HEAD:design.md` in `01-CONTEXT.md`.

### F10 — `expected_artifacts` claims lint that no plan delivers (MEDIUM)
Source: W2, watch item 5. 01-05's `expected_artifacts` describes `ci.yml` as "running lint and the full test suite", but Task 3 specifies no lint job and 01-01 declares no linter — adding one requires editing forbidden `pyproject.toml`, which the stop gate turns into BLOCKED.
**Fix**: strike "lint" from the description and forbid adding a linter until Phase 11, or add `ruff` to 01-01's dev extra now.

### F11 — design.md §17's seven top-level modules are unaddressed (MEDIUM)
Source: G5. 01-01's objective says "the full `src/hermes_auto/` package tree from design.md §17" and its stop gate blocks if the §17 tree "does not match" `files_modified` — but §17 also lists `plugin.py`, `provider.py`, `commands.py`, `cli.py`, `config.py`, `supervisor.py`, and `compatibility.py`, none of which are in `files_modified`. The executor must decide: create, block, or ignore.
**Fix**: state that this plan creates the eleven subpackages only, that the top-level modules arrive in Phases 2–7, and that `compatibility.py` belongs to plan 01-05. Narrow the stop gate.

### F12 — Model-card `transport` could acquire required fields (MEDIUM)
Sources: W5, G7. 01-04's edge case says "if ADR-0003 and design.md §9.2 disagree, follow the ADR". ADR-0003 records candidate identity as a seven-element tuple including `endpoint` and `credential scope`, while 01-04's `transport` requires only three fields. An executor could add required fields, and no gate would catch it.
**Fix**: assert `set(transport['required']) == {'adapter','provider','model'}`; state that identity beyond those three is composed at runtime in Phase 3.

### F13 — Packaged schemas never proven to ship (LOW-MEDIUM)
Source: watch item 5. No plan builds a wheel and inspects it, so all seven schemas can silently fail to package while every in-repo `PYTHONPATH=src` gate stays green.
**Fix**: add a wheel-content check to 01-01 Task 1, re-run after 01-03/01-04 add schema files.

### F14 — `__init__.py` double ownership (LOW)
Source: G9. `tests/contract/__init__.py` and `tests/performance/__init__.py` are now unconditionally created by 01-01 (after the mechanical-pass fix) but still appear in 01-03's and 01-07's `files_modified` with "if 01-01 did not already create it" conditionals.
**Fix**: remove from both plans' `files_modified`, add to their `files_forbidden`, delete the conditionals.

## Risks examined and CLEARED

These were suspected and are not problems — recorded so they are not re-litigated:

1. **POSIX shell on a Windows host** — GNU bash 5.3.15 is present; `for` loops, `$(…)`, `xargs test N -le`, inline `PYTHONPATH=src`, and `'$id'` escaping all execute correctly. Only the three specific command defects in F6 survive, and none are portability issues.
2. **Python 3.11+ / tomllib availability** — 3.11.15 with tomllib; the stop gate will not fire.
3. **The glob-loader parallelism strategy** — sound. 01-03 and 01-04 share no file except the parent directory; `SCHEMA_ROOT` and `repo_root` both resolve correctly. What breaks additivity is the loader's *exception* contract and 01-04's `>=7` gate (F2), not the ownership split.
4. **01-05's `schema-validation` job importing another plan's module** — not an ordering hazard. GitHub Actions evaluates against a committed tree, not mid-wave working state.
5. **ADR/design.md divergence as a general risk** — largely phantom. design.md §9.3 contains the precedence chain verbatim, §7.2 the eight dimensions, §12 exactly twelve reason codes, §5.1 the exact envelope, §13.1 the privacy posture, §15.1 nine baselines, §15.3 nine categories, §21 exactly fourteen threat rows. No invented vocabulary in these plans. One real residual survives as F12.
6. **01-03's `>=3` assertion being flaky on the count** — it is monotonic in the additive direction; 7 ≥ 3 holds. The defect is the exception contract, not the count.

## Recommended actions, in order

1. Fix F1 — set `hermes-agent`, resolve the version range, ungate the nightly install (01-05).
2. Fix F2 — scope all verification to owned files (01-03, 01-04, 01-07 + phase-close gate in CONTEXT).
3. Fix F3 — mandate the wrapper shape, add real schema validation of fixtures (01-07).
4. Fix F4 — require an isolated venv via `user_setup` (01-01, and the interpreter note in all plans).
5. Fix F5 — `sources` parameter, forbid nondeterministic fields, define zero denominators, rename colliding fields (01-07).
6. Fix F6 — replace the three no-op gates (01-02, 01-03, 01-06, 01-05, 01-07).
7. Fix F7 — unconditional schema-validation step + YAML gates (01-05).
8. Fix F8 — `DEBUGGING_AFFINITY` warning (01-04).
9. Fix F9 — commit `.planning/`, pin the design.md blob hash (CONTEXT).
10. Fix F10 — strike "lint" or add `ruff` (01-05 / 01-01).
11. Fix F11 — narrow the §17 stop gate to subpackages (01-01).
12. Fix F12 — assert `transport.required` (01-04).
13. Fix F13 — wheel-content check (01-01).
14. Fix F14 — remove `__init__.py` double ownership (01-03, 01-07).

F1 through F5 must be fixed before execution. F6 through F14 are strongly recommended in the same pass since they are all single-line-to-single-paragraph edits.

---

## Revision Record — 2026-07-26

User selected "Revise all 14 findings" and pinned `SUPPORTED_HERMES = ">=0.19,<1.0"`. All 14 were applied. Post-revision verdict: **OK** — mechanical checks clean, no High-impact decision-completeness gaps remain.

| # | Finding | Resolution | Files touched |
|---|---------|-----------|---------------|
| F1 | Wrong Hermes distribution name, invented version range | `HERMES_DISTRIBUTION_NAME = "hermes-agent"`; `SUPPORTED_HERMES = ">=0.19,<1.0"` recorded as a project decision, not a spec reading; name-guessing stop gate deleted; nightly install gated on `vars.HERMES_DIST_NAME`; test parametrization retargeted to the real range; two new tests (`test_distribution_name_is_hermes_agent`, `test_detect_reads_the_configured_distribution_name`) | 01-05 |
| F2 | Verification scope exceeded file ownership | 01-04 asserts `load_schemas(SCHEMA_ROOT/'routing') == 4`; 01-03 asserts `.../'wire') == 3`; both run only their own test files; 01-07's gate is `pytest tests/performance`; whole-tree assertions moved to a new Phase-Close Gate section in CONTEXT | 01-03, 01-04, 01-07, CONTEXT |
| F3 | `task_id` conditional always fired | Wrapper `{"task_id":…, "event":{…}}` made mandatory in the execution contract; conditional deleted; a real `validate()` command added for every fixture event | 01-07 |
| F4 | Would install into the Hermes Agent venv | `user_setup` added; Task 3 creates `.venv` first and asserts `sys.prefix` lacks `hermes-agent`; interpreter rule stated plan-wide and in CONTEXT; new stop gate; `PYTHONPATH=src` fallback removed as an install substitute | 01-01, CONTEXT |
| F5 | Byte-identical output unachievable | `build_report` gained `sources` (basenames only); timestamp/hostname/platform fields forbidden; every zero denominator defined as `0.0` with `nan`/`inf` banned; `task_count` collision resolved to `event_count`/`distinct_task_count`; three new tests | 01-07 |
| F6 | Three gates verified nothing | `test ! -f X \|\| exit 0` replaced with `git status --porcelain … \| grep -q . && exit 1`; threat-row count anchored to the five-column STRIDE signature; `on:` key read as `w.get('on', w.get(True))` | 01-02, 01-03, 01-05, 01-06, 01-07 |
| F7 | CI job content unverified, incoherent clause | Tolerance clause replaced with one unconditional assertion; YAML gates added for `schema-validation`, `permissions`, `concurrency`, and the `report` job | 01-05 |
| F8 | design.md §12 example emits a code absent from the enum | Explicit instruction not to copy `DEBUGGING_AFFINITY` and not to widen the enum; negative grep added | 01-04 |
| F9 | Plans not pinned to a design.md revision | New Design Document Pinning section with the commit-and-record procedure | CONTEXT |
| F10 | `expected_artifacts` claimed lint nothing delivers | "lint" struck from the description; linter additions forbidden in both plans; negative grep on `ci.yml` and on the dev extra | 01-05, 01-01 |
| F11 | §17's seven top-level modules unaddressed | Scope note added; stop gate narrowed to the eleven subpackages; negative existence check | 01-01 |
| F12 | `transport` could acquire required fields | `transport.required` fixed at exactly three fields with a command and a test; the general "follow the ADR" edge case replaced with a bounded rule | 01-04 |
| F13 | Packaged schemas never proven to ship | Wheel-content check added to 01-01 Task 3 and a seven-schema wheel assertion to the phase-close gate | 01-01, CONTEXT |
| F14 | `__init__.py` double ownership | `tests/contract/__init__.py` and `tests/performance/__init__.py` removed from 01-03/01-07 `files_modified`, added to their `files_forbidden`, conditionals deleted; 01-01's `files_modified` extended to the seven files it actually creates | 01-01, 01-03, 01-07 |

Additional corrections made during revision, beyond the 14:

- 01-02's "all five rows of the design.md §3 table" was wrong — the table's fifth row is the *recommended* approach, so only four are rejected alternatives. Corrected with a count gate.
- 01-07's `depends_on` was incomplete: it reads `docs/privacy.md` (01-06) and its suite touched 01-05's tests. Now `["01-01","01-03","01-04","01-05","01-06"]` with per-plan prerequisite checks that name the responsible plan.
- 01-03 gained a `test_non_object_schema_raises` case — a top-level JSON array would previously have raised `TypeError` instead of `SchemaLoadError`.
- 01-06's STRIDE summary table was re-specified to two columns so it cannot inflate the threat-row count.
- A shell rule was added plan-wide: verification commands are POSIX and must run through Bash, never PowerShell.

Post-revision mechanical state: `verification_commands` present and non-empty on all 7 (6–11 each); zero `files_modified`/`files_forbidden` overlaps; zero same-wave file overlaps; all 7 carry `<execution_contract>`, `<stop_gates>`, `<recovery>` and the four harness terms plus `BLOCKED`.

Deliberately left as-is: 01-01 runs `pytest tests/ -q`, which is correct — it is the sole wave-1 plan that creates tests, and no other plan's tests exist at that point.

---

## F1 Follow-Up — upstream resolved, 2026-07-27

The user supplied the Hermes location: **`https://github.com/NousResearch/hermes-agent`**. Verified against the repository and PyPI:

| Check | Result |
|-------|--------|
| Upstream `pyproject.toml` `name` | `hermes-agent` — confirms the F1 correction |
| Upstream `pyproject.toml` `version` | `0.19.0` — **matches the local install exactly** |
| PyPI | `hermes-agent==0.19.0`, publicly installable |
| Upstream `requires-python` | `>=3.11,<3.14` |
| License / default branch | MIT / `main` |
| Git tags | CalVer: `v2026.7.20`, `v2026.7.7.2`, `v2026.6.19`, … |

**F1's version range is now evidence-based rather than invented.** `SUPPORTED_HERMES = ">=0.19,<1.0"` has a lower bound taken directly from upstream's own declared version, confirmed by three independent sources. The upper bound remains a project decision — design.md still states no bounds anywhere — and the summary must continue to say so.

**New finding (F15) — CalVer/semver mismatch.** Hermes's git *tags* are CalVer while its *distribution* version is semver. `importlib.metadata.version("hermes-agent")` returns `0.19.0`, never `2026.7.20`. This is a live trap: a maintainer who checks the releases page would reasonably conclude the pin is wrong and "correct" `SUPPORTED_HERMES` to something like `>=2026.1`, which would match nothing and make the probe report `UNSUPPORTED_VERSION` for every real Hermes install — silently disabling the compatibility gate the whole plan exists to provide. Mitigated in three places: an inline comment in `compatibility.py`, a prominent note in `01-CONTEXT.md`'s resolved-facts table, and a regression test `test_calver_tag_is_not_treated_as_a_version` that asserts `"2026.7.20"` reports `UNSUPPORTED_VERSION`, so the mistake breaks a test with an explanation attached rather than passing silently.

**C4 (assumption) and F1's CI half are now closed.** Both repository variables were provisioned:

```
HERMES_DIST_NAME = hermes-agent
HERMES_GIT_URL   = https://github.com/NousResearch/hermes-agent
```

The nightly `latest-release` job will genuinely `pip install hermes-agent` from PyPI, and `hermes-main` will genuinely install from `git+…@main`. The variable indirection and the `report` job's fail-when-unset guard both stay — the distribution name is upstream-controlled, and a future variable deletion must not silently produce a vacuous green.

**New open item.** Hermes caps at `requires-python = ">=3.11,<3.14"` while this project declares `>=3.11` with no upper bound. That is correct for the standalone gateway, which never imports Hermes — but the optional Tier-4 credential bridge in Phase 7 does, so the cap may need mirroring then. Recorded in STATE.md rather than changed now.
