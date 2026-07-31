# Phase 1: Contracts, Schemas & Baselines — Context

## Phase Goal
Freeze every public contract and establish a reproducible fixed-model baseline before any routing logic exists.

This phase corresponds to `design.md` §18 Phase 0 and PR 1 of the §19 pull-request sequence. Its defining exit criterion is negative: **no production routing code may be introduced.** Everything here is packaging, schema, documentation, CI, and measurement scaffolding that later phases build against.

## Requirements Covered

> `.planning/REQUIREMENTS.md` does not exist yet (it is created between milestones). Requirement descriptions below are taken from `.planning/PROJECT.md` § Requirements § Active, which carries the full text.

- **R26**: Frozen public contracts before routing logic — ADRs, versioned schemas, threat model, and a reproducible fixed-model baseline corpus. *(Primary requirement for this phase.)*
- **R1** (schema surface only): the `_hermes_auto` routing-metadata envelope that the provider plugin will send via `build_extra_body()`. Phase 1 defines the schema; Phase 2 implements the provider.
- **R6** (schema surface only): the model-card schema with capabilities, affinities, limits, economics, harness, policy, and evidence blocks. Phase 1 defines the schema; Phase 3 implements loading and validation.
- **R14** (schema surface only): the `RouteDecision` record and its reason-code enumeration. Phase 1 defines the schema; Phase 4 implements decision generation.
- **R19** (schema surface only): the outcome-event schema for the local telemetry store. Phase 1 defines the schema; Phase 8 implements the SQLite store.

## What Already Exists (from prior phases)
Nothing — this is the first phase of the project. The repository currently contains only:

- `design.md` — the complete 2444-line design document that is the source of truth for this phase. **Read-only in every plan.** No plan may modify it.
- `.git/` — initialized, on branch `main`, remote `https://github.com/9thLevelSoftware/hermes-auto.git`
- `.planning/` — PROJECT.md, ROADMAP.md, STATE.md from `/legion:start`

There is no `src/`, no `tests/`, no `pyproject.toml`, and no CI. Plan 01-01 creates that foundation; every other plan writes into it.

## Key Design Decisions

**Why 7 plans instead of the ROADMAP estimate of 3.** Phase 1 freezes six independent contract surfaces — packaging, ADRs, wire protocol schemas, routing schemas, compatibility/CI, threat model, and the baseline harness. Each has a different owner, a different verification boundary, and a different downstream consumer. Collapsing them into 3 plans would force executors to make schema and CI design decisions mid-plan, violating the decision-complete contract rule. ROADMAP plan counts are estimates, not caps.

**Why the schema work is split across two plans (01-03, 01-04).** Wire protocol schemas describe what crosses the Hermes↔gateway boundary and are validated by protocol contract tests. Routing schemas describe the router's internal evidence records and are validated by data-shape tests. They have different reviewers and different failure modes. To let them run in parallel, the schema loader in 01-03 discovers schemas by **directory glob** rather than by an explicit registry list — so 01-04 adds files without editing any file 01-03 owns.

**Why ADRs are Wave 1 rather than Wave 0.5.** ADR-0003 (model-card architecture) and ADR-0004 (telemetry and privacy) are inputs to the 01-04 schemas, and ADR-0004 is an input to the 01-06 privacy documentation. Putting ADRs in Wave 1 alongside the skeleton — which touches entirely disjoint files — gets them written before anything depends on them without serializing the phase.

**Why `docs/` is owned entirely by 01-02 in Wave 1.** The repository skeleton (01-01) deliberately does *not* create `docs/` stubs. If it did, 01-02 would have to overwrite files 01-01 owns, creating a same-wave write conflict. Instead 01-01 owns packaging and tests; 01-02 owns documentation. They run in parallel safely.

**Why all runtime and test dependencies are declared in 01-01.** `compatibility.py` (01-05) needs `packaging`; the schema tests (01-03, 01-04) need `jsonschema`; the baseline harness (01-07) needs `pyyaml`. Rather than have four plans each edit `pyproject.toml` — a guaranteed conflict — 01-01 declares the complete dependency set up front and every later plan treats `pyproject.toml` as forbidden.

**Why the coordinator is on 01-01.** Per agent-registry Step 6, a phase spanning four divisions (Engineering, Testing, Product, Specialized) must include a coordinator on at least one plan. 01-01 is the natural host: it establishes the traceability structure the whole phase writes into.

**Architecture proposals**: skipped by user — `design.md` §17 already fixes the repository layout and §18 Phase 0 already fixes the work items, so competing proposals would re-litigate settled decisions.

**Spec pipeline**: skipped by user — `design.md` is already the specification for this phase.

**Codebase map**: unavailable — greenfield repository with no source code. `/legion:map` becomes useful from Phase 2 onward.

## Plan Structure

- **Plan 01-01 (Wave 1)**: Repository skeleton & packaging contract — `pyproject.toml`, the full `src/hermes_auto/` package tree per design.md §17, repo meta files, and the eight-directory `tests/` tree with pytest configuration.
- **Plan 01-02 (Wave 1)**: Architecture decision records — an ADR template plus ADR-0001 through ADR-0005, and the `docs/architecture.md` index that links them.
- **Plan 01-03 (Wave 2)**: Wire protocol schemas & schema loader — versioned JSON Schemas for the OpenAI chat-completions request/response subset and the streaming SSE contract, plus a glob-based loader.
- **Plan 01-04 (Wave 2)**: Decision, model-card & outcome-event schemas — versioned JSON Schemas for `_hermes_auto` metadata, `RouteDecision`, model cards, and outcome events.
- **Plan 01-05 (Wave 2)**: Hermes compatibility pinning & CI — a fail-closed version probe plus the main CI workflow and the nightly Hermes-`main` compatibility workflow.
- **Plan 01-06 (Wave 2)**: Threat model & privacy documentation — all 14 rows of the design.md §21 threat table mapped to mitigating modules, plus the user-facing privacy guide.
- **Plan 01-07 (Wave 3)**: Baseline corpus & measurement harness — the fixed-model task corpus, `scripts/benchmark.py`, a determinism test, and the baseline report generator.

## Wave Dependency Graph

```
Wave 1        01-01 (skeleton)          01-02 (ADRs)
                 │      │      │            │    │
                 ▼      ▼      ▼            ▼    ▼
Wave 2        01-03  01-04  01-05        01-04  01-06
                        │
                        ▼
Wave 3               01-07 (also depends on 01-01)
```

## Resolved Environment Facts

Verified during planning. Plans treat these as given — do not re-derive or guess.

| Fact | Value | Consumed by |
|------|-------|-------------|
| Hermes repository | **`https://github.com/NousResearch/hermes-agent`** — MIT, default branch `main` | 01-05 |
| Hermes distribution name | **`hermes-agent`** (`hermes` raises `PackageNotFoundError`) | 01-05 |
| Hermes version | `0.19.0` — identical across the repo's `pyproject.toml`, PyPI, and the local install | 01-05 |
| Published on PyPI | **Yes**, `hermes-agent==0.19.0` — so `pip install hermes-agent` genuinely works in CI | 01-05 |
| Hermes `requires-python` | `>=3.11,<3.14` | 01-01, 01-05 |
| Supported range | **`">=0.19,<1.0"`** — now evidence-based: `NousResearch/hermes-agent` `pyproject.toml` declares `version = "0.19.0"`. design.md still states no bounds, so the *upper* bound remains a project decision. | 01-05 |
| GitHub repo variables | `HERMES_GIT_URL` and `HERMES_DIST_NAME` — **provisioned 2026-07-27** | 01-05 |

**Version-scheme trap — read before touching `SUPPORTED_HERMES`.** Hermes's *git tags* are CalVer (`v2026.7.20`, `v2026.7.7.2`, `v2026.6.19`, …) but its *distribution version* is semver `0.19.0`. `importlib.metadata.version("hermes-agent")` returns `0.19.0`, never `2026.7.20`. Anyone who checks the repo's releases page will see CalVer and may conclude the pin is wrong and "fix" `SUPPORTED_HERMES` to something like `>=2026.1`. That would make the specifier match nothing and the probe report `UNSUPPORTED_VERSION` for every real Hermes install. The probe compares against the **distribution** version. Do not switch the range to CalVer.
| Machine default `sys.prefix` | `C:\Users\dasbl\AppData\Local\hermes\hermes-agent\venv` — the Hermes Agent venv | 01-01 |
| `pytest` in the default interpreter | ABSENT, so an install will be triggered | 01-01 |
| Python | 3.11.15, `tomllib` present | all |
| Already installed | `jsonschema` 4.26.0, `pyyaml` 6.0.3, `packaging` 26.0 | 01-01 |
| Shell | GNU bash 5.3.15 available | all |

**Interpreter rule.** Plan 01-01 creates `.venv` as its first act and every subsequent command in every plan uses `.venv/Scripts/python.exe` (Windows) or `.venv/bin/python` (POSIX). Never install into the machine default — it is the Hermes Agent environment, and installing there violates PROJECT.md's isolation constraint.

**Shell rule.** All verification commands are POSIX shell. Run them through the Bash tool, never PowerShell: `test`, `for … done`, `$(…)`, and inline `VAR=x cmd` prefixes do not exist in PowerShell.

## Design Document Pinning

Every plan cites `design.md` by section number (§7.2, §12, §15.1, §17, §20.3, §21). Section numbers shift if a section is inserted, which would silently invalidate those citations.

**Pinned `design.md` blob**: `18bb54b36485fa0813ec67f84a74628a9eee3aae`
**Pinned at commit**: `331a69d` ("Create design.md")
**Recorded**: 2026-07-27

Every `§N` citation in this phase's seven plans refers to `design.md` at exactly that blob. Verify it before execution:

```bash
test "$(git rev-parse HEAD:design.md)" = "18bb54b36485fa0813ec67f84a74628a9eee3aae" \
  && echo "design.md matches the pinned blob" \
  || echo "DESIGN DOC CHANGED — re-verify every section citation before continuing"
```

A blob hash is content-addressed, so it is stable across branches, rebases, and rewritten commits — only an actual edit to `design.md` changes it.

If the hash no longer matches, do not proceed on the assumption that section numbers held. Inserting one section renumbers every later citation silently, and the plans cite §5.1, §5.3, §5.4, §7.2, §7.5, §8.2, §9.1, §9.2, §9.3, §10.2, §12, §13.1–§13.5, §15.1–§15.3, §17, §18, §20.3, §20.6, §21, §22, and §23. All seven plans list `design.md` in `files_forbidden` and gate on `git diff --exit-code design.md`, so accidental edits *during* execution are caught — but a deliberate revision *between* planning and execution is not, and this hash is the only thing that detects it.

## Phase-Close Gate

Individual plans deliberately scope their verification to **files they own**, because plans 01-03 and 01-04 run in parallel and share a schema directory, and because `load_schemas()` raises on any malformed file anywhere beneath its root. A plan asserting on the whole tree would fail on another plan's in-progress work and emit a false `BLOCKED`.

The whole-phase assertions therefore live here and run **once, after all seven plans complete**:

```bash
# All seven schemas discoverable through the loader
PYTHONPATH=src python -c "from hermes_auto.gateway.schemas import load_schemas; s=load_schemas(); assert len(s)==7, sorted(s)"

# Full suite green across every plan
python -m pytest tests/ -q

# design.md untouched
git diff --exit-code design.md

# Baseline report reproducible and free of volatile fields
PYTHONPATH=src python scripts/benchmark.py --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-a.json \
  --runs tests/fixtures/baseline/recorded-run-b.json --format json > /tmp/r1.json
PYTHONPATH=src python scripts/benchmark.py --corpus tests/fixtures/baseline/corpus.yaml \
  --runs tests/fixtures/baseline/recorded-run-b.json \
  --runs tests/fixtures/baseline/recorded-run-a.json --format json > /tmp/r2.json
cmp /tmp/r1.json /tmp/r2.json

# Wheel actually ships the schema payload added by 01-03 and 01-04
python -m pip wheel --no-deps -w dist-check . && python -c "import zipfile,glob; ns=zipfile.ZipFile(glob.glob('dist-check/*.whl')[0]).namelist(); n=[x for x in ns if x.endswith('.schema.json')]; assert len(n)==7, n"
```

The last check matters: `package-data` globs are a common silent packaging failure. Without it, all seven schemas could fail to ship while every in-repo `PYTHONPATH=src` gate stayed green, and `load_schemas()` would return `{}` from an installed copy.

## Phase-Wide Constraints

Every plan in this phase must honor these, drawn from PROJECT.md § Constraints:

1. **No production routing logic.** Feature extraction, scoring, eligibility filtering, and candidate selection are Phase 3–4 work. Module files created here are package placeholders, not implementations.
2. **`design.md` is read-only.** It is the source of truth and appears in `files_forbidden` for every plan.
3. **No Hermes core files are touched.** This repository is standalone; nothing here reaches into a Hermes checkout.
4. **No raw prompt retention anywhere**, including in fixtures and baseline corpora. Fixtures use synthetic content only.
5. **No secrets in any committed file.** Credential references only (`env:VAR_NAME`), never values.
6. **Behavioral settings belong in config, not environment variables.** Only credentials and generated local tokens are environment-sourced.
