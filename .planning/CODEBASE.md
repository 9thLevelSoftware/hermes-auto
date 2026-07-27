# Codebase Map — Hermes Auto Router

```yaml
map_schema_version: 1
generated_at: 2026-07-27
analyzed_commit: 3c9312f
source_file_count: 80
source_fingerprint: 1fa41019261f2911
scope: full-project
index_chunks: 213
```

## What this repository is today

Phase 1 of 11 is complete and reviewed; Phase 2 is planned but **not built**. So the code you will find is deliberately *contracts without behavior*: seven versioned JSON Schemas, a schema loader, a fail-closed Hermes version probe, and an offline evaluation harness. Phase 1's defining exit criterion was negative — no production routing code — and it holds.

**Do not read the module tree as an indication of what exists.** Eight of the eleven subpackages under `src/hermes_auto/` are one-line docstrings reserving a name for a later phase. Only six Python modules carry real logic.

## Detected stack

| | |
|---|---|
| Language | Python `>=3.11` (developed on 3.11.15) |
| Packaging | setuptools, src-layout, distribution `hermes-auto-router` |
| Runtime deps | `packaging`, `jsonschema`, `pyyaml`, `referencing` |
| Dev deps | `pytest`, `pytest-asyncio`, `hypothesis` |
| Tests | pytest, 206 passing, 8-directory tree |
| CI | GitHub Actions — 3 OS × 2 Python matrix, schema validation, wheel packaging |
| Interpreter | **`<repo>/.venv/Scripts/python.exe`** — the machine default is the Hermes Agent venv |

No HTTP framework, client, or async server yet. Phase 2 adds `starlette`, `uvicorn[standard]`, `httpx`.

## Module structure

### Modules with real content — read these first

| Module | Lines | Responsibility |
|---|---|---|
| `src/hermes_auto/evaluation/metrics.py` | 410 | Deterministic aggregation of quality, cost, latency, cache, and tool-call metrics. Guards every division; unknown cost is counted, never summed as zero |
| `scripts/benchmark.py` | 299 | CLI producing byte-identical baseline reports. Validates events, rejects non-finite JSON literals at parse |
| `src/hermes_auto/evaluation/baselines.py` | 293 | The nine `design.md` §15.1 strategies, report shape, JSON and markdown renderers |
| `src/hermes_auto/gateway/schemas.py` | 279 | **Highest fan-in module.** Glob schema discovery, `build_validator`, `build_registry`, `EXPECTED_SCHEMA_IDS` |
| `src/hermes_auto/evaluation/corpus.py` | 235 | Baseline task corpus loader, nine workload categories, `require_mapping` shape guard |
| `src/hermes_auto/compatibility.py` | 174 | Fail-closed Hermes probe pinning `hermes-agent >=0.19,<1.0` |

### Reserved names — one-line docstrings only

`routing/`, `state/`, `inventory/`, `health/`, `adapters/`, `harnesses/`, `telemetry/`, `learning/`, and `gateway/__init__.py`. Each names the phase that fills it. `data/` holds the packaged schema tree.

## Data and schema map

Seven versioned JSON Schemas under `src/hermes_auto/data/schema/`, all discovered by recursive glob and keyed by `$id`:

**`wire/`** — what crosses the Hermes↔gateway boundary
- `openai-chat-request.v1` — request subset; tools, multipart content, streaming flags; `_hermes_auto` is a `$ref`
- `openai-chat-response.v1` — non-streaming response with cached and reasoning token detail
- `sse-stream-contract.v1` — one streamed chunk; empty deltas valid, `index` is the tool-call assembly key

**`routing/`** — the router's own evidence records
- `hermes-auto-metadata.v1` — the `_hermes_auto` envelope, `protocol_version` const 1
- `route-decision.v1` — hashed session identity, 8-dimension requirement vector, exactly 12 reason codes
- `model-card.v1` — 8 required capabilities bounded 0–1, nullable economics with **no default**
- `outcome-event.v1` — `additionalProperties: false` over a 23-field allowlist

Phase 2 adds an eighth, `wire/openai-error.v1`.

## Conventions detected

- **Frozen vocabularies are byte-exact.** Eight capability dimensions, ten domain tags, twelve reason codes, nine baseline strategies, nine corpus categories. All trace to a `design.md §N` citation. Changing one is a contract break.
- **Value constraints, not absolute claims.** Every schema description states what is *bounded* and explicitly notes that value bounds constrain shape, not intent. A test walks all descriptions against 11 banned phrasings. This was learned the hard way across three review cycles.
- **Hoisted validators.** `validate()` re-runs `check_schema` at ~50 ms/call. Use `build_validator` on any path that runs more than once.
- **Absent ≠ corrupt.** Recurring idiom: a missing file returns `None`/defaults; a present-but-malformed file raises a typed error.
- **Unknown is never zero.** Null cost is counted separately, never summed. The single most load-bearing correctness rule in `metrics.py`.
- **Errors are typed, not leaked.** `SchemaLoadError`, `CorpusError`, `ConfigError` — never a bare `KeyError`, `TypeError`, or `json.JSONDecodeError` escaping a public function.
- **Determinism.** Reports are byte-identical across runs, argument order, and working directory. Fixed-precision floats, no timestamps, sorted iteration.
- **Verification is executable.** Every plan task carries `> verification:` bash lines. Prose claims without a runnable check have twice been found vacuous.

## Agent guidance

**Preferred**
- Read `gateway/schemas.py` before touching anything schema-adjacent — it is the highest fan-in module and the loader other phases depend on
- Use `./.venv/Scripts/python.exe`, never the machine default
- POSIX shell through the Bash tool; PowerShell will fail on `test`, `for … done`, and inline `VAR=x cmd`
- Cross-plan guards use `git status --porcelain -- <path> | grep -q . && exit 1 || exit 0`

**Avoid**
- `validate()` in a loop — use `build_validator`
- Adding a dependency without checking `pyproject.toml`; the set is deliberately small and each addition was argued
- Absolute containment claims in any schema description — a test rejects them
- `git diff --quiet` as a cross-plan guard; it cannot detect an untracked file

**Touch with care**
- `src/hermes_auto/data/schema/**` — frozen contracts consumed by Phases 3, 4, and 8. A change ripples into `EXPECTED_SCHEMA_IDS`, the CI identity check, and the wheel gate together
- `pyproject.toml` `[tool.setuptools.package-data]` — the schema globs fail silently; only a built wheel proves them
- `design.md` — read-only, pinned at blob `18bb54b36485fa0813ec67f84a74628a9eee3aae`

## Risk areas

| Area | Risk | Why |
|---|---|---|
| `data/schema/**` | **High** | Seven frozen contracts. Phase 1's review found a `model-card.id` left unbounded while every consumer was tightened — a card validated but its decision record did not |
| `pyproject.toml` package-data | **High** | Globs fail silently; every in-repo `PYTHONPATH=src` gate stays green while an installed wheel ships nothing. Guarded by a CI `package` job that installs non-editable |
| `gateway/schemas.py` | **Medium** | `$ref` resolution is registry-scoped: passing a *subset* mapping raises `referencing.exceptions.Unresolvable`, which is **not** a `ValidationError`. Phase 2 gateway code must call bare `load_schemas()` |
| `evaluation/metrics.py` | **Medium** | JSON Schema `integer` matches `4200.0`, so schema validation cannot catch float counts. `_as_count`'s narrowing is the only guard |
| `compatibility.py` | **Medium** | Hermes tags are CalVer (`v2026.7.20`) but the distribution is semver `0.19.0`. A CalVer range matches nothing — guarded by `test_calver_tag_is_not_treated_as_a_version` |
| Windows paths | **Medium** | `os.chmod` is a no-op on ACLs. Any permission write needs an `icacls` readback |

## Test map

| Directory | Contents |
|---|---|
| `tests/contract/` | Wire and routing schema conformance, loader failure modes, privacy-invariant negatives |
| `tests/unit/` | Package layout, compatibility probe, benchmark CLI (24 tests) |
| `tests/performance/` | Baseline determinism, no-`nan`, working-directory independence |
| `tests/fixtures/` | `wire/`, `routing/`, `baseline/` — all synthetic, no real credentials or model ids |
| `tests/{property,integration,e2e,fault}/` | Reserved; populated from Phase 2 |

Run: `./.venv/Scripts/python.exe -m pytest tests/ -q` → 206 passed.

## Setup

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
./.venv/Scripts/python.exe -m pytest tests/ -q
```

The machine default interpreter is the **Hermes Agent venv** (`…\hermes-agent\venv`). Installing there violates the project's own isolation constraint — always use `.venv`.

## External integration

Hermes Agent lives at `https://github.com/NousResearch/hermes-agent` (MIT), locally checked out at `C:/Users/dasbl/AppData/Local/hermes/hermes-agent`. It is a **source checkout, not an installed distribution** — `import providers` fails from this venv. Verification that needs a real `ProviderProfile` must subprocess against Hermes's own interpreter with `cwd` at the checkout root.

Model providers are discovered by **directory scan** of `$HERMES_HOME/plugins/model-providers/`, not pip entry points. Control plugins use the `hermes_agent.plugins` entry group **and** require `plugins.enabled` opt-in.

## Map artifacts

| File | Purpose |
|---|---|
| `.planning/CODEBASE.md` | This document |
| `.planning/codebase/index.jsonl` | 213 retrievable chunks — modules, symbols, schemas, docs |
| `.planning/codebase/symbols.json` | Per-file symbol table and import edges |
| `.planning/codebase/search.md` | Consumer search protocol |
| `.planning/config/directory-mappings.yaml` | File-placement rules for plan validation |
