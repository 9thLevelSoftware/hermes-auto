# Project State

## Current Position
- **Phase**: 2 of 11 (executing — wave 2 of 5 complete)
- **Status**: Phase 2 wave 2 complete — 5/9 plans, 622 tests passing (was 206)
- **Last Activity**: Phase 2 wave 2 execution (2026-07-27)

## Progress
```
[#####···············] 23% — 12/52 plans complete
```

## Phase 1 Review
Passed after 3 cycles with a 4-reviewer dynamic panel. **6 blockers and 29 warnings found and
resolved**; none left unresolved. Tests 70 → 206.

The governing finding: three cycles of security review converged on *overclaiming*, not
under-constraining. `additionalProperties: false` plus value constraints bound the **shape** of
what can be written, not the **intent** — a 40-character GitHub token is indistinguishable from a
legitimate identifier, and 17 of 26 smuggling payloads survived the first tightening. Every
absolute "cannot carry X" claim was replaced with a precise statement of what is bounded plus
explicit scoping to the Phase 8 write path, and a test now walks all schema descriptions against
11 banned phrasings. Ten vectors remain open by design, asserted as open in a green test.

Full record: `.planning/phases/01-contracts-schemas-baselines/01-REVIEW.md`

## Phase 1 Results
All 7 plans Complete. 196 verification commands run across the phase, 195 passed. The single
failure was a defect in the plan text, not the work: plan 01-02's cross-plan guard named
`README.md` and `SECURITY.md`, which plan 01-01 legitimately owns in the same wave. Fixed in
commit `c340720` before Wave 2, which would otherwise have hit the same false positive twice.

Phase-close gate (all green):
- 7 schemas discoverable through the loader
- 70 tests pass
- `design.md` matches its pinned blob and is unchanged
- Baseline report byte-identical under reversed argument order, no `nan`
- A built wheel carries all 7 schema files plus `py.typed` — closes critique finding F13

## GitHub
- **Phase 2 issue**: [#3](https://github.com/9thLevelSoftware/hermes-auto/issues/3) — Phase 2: Provider Plugin & Passthrough Gateway
- **Phase 1 PR**: [#2](https://github.com/9thLevelSoftware/hermes-auto/pull/2) — merged to main
- **Phase 1 issue**: [#1](https://github.com/9thLevelSoftware/hermes-auto/issues/1) — Phase 1: Contracts, Schemas & Baselines

## Recent Decisions
- **Design source**: initialized from `design.md` (repo root) — a complete specification covering product definition, architecture, configuration, repository layout, Phase 0-10 staging, test plan, and threat model
- **Codebase map**: unavailable — no source code present at initialization; run `/legion:map` once implementation begins
- **Execution mode**: Guided — Legion recommends actions, user approves before each step
- **Planning depth**: Deep Analysis — 11 phases mirroring the design document's own staging
- **Cost profile**: Premium — Opus for planning and execution, Sonnet for checks
- **Integration boundary**: provider plugin plus local gateway sidecar; no Hermes core modification
- **Learning sequence**: deterministic-first; ML deferred until Phase 8 produces a measured baseline
- **Phase 1 sizing**: 7 plans rather than the roadmap's estimated 3 — six independent contract surfaces with different owners and verification boundaries
- **Hermes upstream**: `https://github.com/NousResearch/hermes-agent` (MIT, default branch `main`)
- **Hermes distribution**: `hermes-agent` 0.19.0 — consistent across the upstream `pyproject.toml`, PyPI, and the local install; `hermes` does not exist
- **Supported Hermes range**: `>=0.19,<1.0` — lower bound is the upstream declared version (evidence-based); upper bound is a project decision, since `design.md` states no bounds anywhere
- **Version-scheme trap**: Hermes git tags are CalVer (`v2026.7.20`) but the distribution is semver `0.19.0`. The probe compares the distribution version. A CalVer range would match nothing — guarded by `test_calver_tag_is_not_treated_as_a_version`
- **Environment isolation**: the machine default interpreter IS the Hermes Agent venv, so plan 01-01 creates a dedicated `.venv` and every command uses it

## Phase 2 Plan — verified corrections to design.md
Three read-only architecture agents inspected the real Hermes source; the coordinator re-verified the load-bearing claims. Four `design.md` §5.1 statements are wrong and are corrected in `02-CONTEXT.md` § VERIFIED HERMES FACTS:
- Model providers are discovered by **directory scan** of `$HERMES_HOME/plugins/model-providers/`, not pip entry points — `pip install` never registers the provider
- `ProviderProfile` is a `@dataclass`: subclass for method overrides, then **instantiate with kwargs** and call `register_provider(instance)`
- The control plugin requires **`plugins.enabled`** opt-in or `register()` is never called
- Hermes is a **source checkout**, not an importable distribution — verification must subprocess against its own interpreter

Architecture: **Pragmatic** (Starlette + uvicorn + httpx, async, opaque-dict passthrough). Rejected `BackendAdapter`-now — Phase 7 shapes it against three real targets.

Critique verdict REWORK at 19% completeness; five execution-breaking findings auto-refined, including an admin listener nothing started and a `session_id or ""` that would 400 every auxiliary call. 13 of 22 gaps were vacuous verification commands — Phase 1's finding F6 at 4x density.

## Phase 2 Wave 1 Results
All 3 plans Complete with Warnings. 68 verification commands run, 65 passed.

**All three failures were the same guard defect**, reported independently by all three agents and
fixed in plans 02-04 … 02-09 before Wave 2 dispatch. The whole-tree `git status --porcelain -- <many
paths>` idiom asserts a *phase*-level property from inside one plan: during a parallel wave it can
only report siblings' legitimate work, and 02-02's version contradicted its own plan's authorized
`EXPECTED_SCHEMA_IDS` edit — it would have failed with no siblings running at all. Replaced with a
frozen-asset guard over 12 paths no Phase 2 plan owns, plus the `design.md` blob pin. Proven
non-vacuous: fails on a touched routing schema, passes when restored, ignores a Wave 2 deliverable.

**Highest-value find**: the repo has `core.autocrlf=true` and had no `.gitattributes`. Git was proved
to rewrite `
` → `

` on checkout of a `.txt` file, so every SSE frame terminator would have
differed between commit and checkout — invisible locally, breaking 02-04/02-08/02-09 in CI. Plan
02-03 shipped an unplanned `tests/fixtures/sse/.gitattributes` and proved the fix by deleting all 12
fixtures and restoring them from git byte-identically.

**Vacuous verifications, third phase running.** Roughly 18 of 51 inline checks could pass while the
work was wrong. The two sharpest: `V10` in 02-03 was `print([...] or 'complete')`, which exits 0
whether or not the interface is missing methods; and `assert 'hmac' in getsource(m)` in 02-02 is
satisfied by the `import hmac` line alone — proved empirically that unsalted `sha256(SALT + sid)`
passes it while being exactly the construction the plan forbids. Every one was run as written *and*
replaced with a stronger check.

## Open Items — raised by Phase 1 execution
- **Phase 2**: no error-envelope schema exists. `wire/openai-error.v1.schema.json` is needed — context errors return the OpenAI error body, a different shape from `chat.completion`, currently unvalidated.
- **Phase 2**: `pytest-asyncio` resolved to 1.4.0, which no longer defaults to a usable mode. Needs `asyncio_mode` in `[tool.pytest.ini_options]` or explicit markers before the first async test.
- **Phase 8**: outcome-event `v2` should add a health-score field (`health_observation` events are currently well-formed but carry no observation), plus the four design.md §13.3 signals not representable in v1.
- **Phase 8**: `docs/privacy.md` promises that deleting the SQLite file suffices — so no shadow copy, cache, or index may live outside it. Worth an explicit test.
- **Phase 11**: wheel-content check should become a standing CI job. `package-data` globs fail silently while every in-repo `PYTHONPATH=src` gate stays green.
- **Phase 11**: fill the `SECURITY.md` security contact (currently `TODO`).
- CI matrix tests Python 3.11/3.12; Hermes supports through 3.13. Adding a `3.13` cell would close the gap.
- **Phase 2**: salting has no Phase 1 structural backstop. `telemetry/events.py` must apply a per-install salt before the digest is written, and the test that pins it (same session id under two salts → two digests) lands with that code. `docs/privacy.md` now says so explicitly.
- **Phase 2 plan authoring**: do not copy plan 01-06's `git diff --quiet` guard idiom — it cannot detect an untracked forbidden file. Use `git status --porcelain -- <path> | grep -q . && exit 1 || exit 0`.
- **Phase 2**: candidate ids now permit `/` (segmented pattern), so `../../etc/passwd` matches. Any consumer using a candidate id as a path component must sanitize it; the model-card description says so.
- **Phase 3**: run `/legion:map` before planning — Phase 1 delivered 36 Python files and Phase 2 adds a full HTTP service, past the point a plan author can hold it all
- ~~**Phase 3**: flip `gateway.strict_validation` if hoisted validation measures under 2 ms~~ — **RESOLVED, keep it off.** Plan 02-04 measured 29.06 ms mean / 31.79 ms p95 on a 155 KB body: 14x the flip condition, and a third of the entire 100 ms TTFT budget. Hoisting works (69.10 -> 29.06 ms) but does not rescue it. The metadata envelope at 41 us is hot-path-safe.
- **Phase 3**: candidate ids permit `/`, so sanitize before using one as a path component — deferred from Phase 1 as N/A there
- **Phase 2 known gaps** (recorded, not fixed): `hermes auto models|benchmark|explain|export-diagnostics` are deferred to Phases 3, 8, 4, 11; "no CORS" is asserted nowhere; `docs/privacy.md` still says salting is "Left to Phase 8" though plan 02-02 delivers it
- **Before going public**: sweep `.planning/` for absolute paths containing the developer's OS username.

## Resolved by Phase 1
- ~~Mirror Hermes's `requires-python` upper bound?~~ — **No.** Plan 01-05's analysis: capping this project at `<3.14` would make it uninstallable on 3.14 for every user, including the majority who never enable the optional bridge, inverting design.md §10.2's "should remain optional". Use an optional extra in Phase 7 (`hermes-bridge = ["hermes-agent>=0.19,<1.0"]`) so pip enforces Hermes's ceiling transitively at the right scope. Upper bounds are baked into published metadata and cannot be relaxed retroactively.

## Resolved
- ~~Provision `HERMES_DIST_NAME` and `HERMES_GIT_URL`~~ — both set 2026-07-27; the nightly workflow will genuinely measure against PyPI `hermes-agent` and `NousResearch/hermes-agent@main`
- ~~Pin the `design.md` revision~~ — blob `18bb54b36485fa0813ec67f84a74628a9eee3aae` at commit `331a69d`, recorded in `01-CONTEXT.md` with a verification command

## Next Action
Wave 3 of Phase 2 in progress (plan 02-06 admin API)

## Open Items — raised by Phase 2 Wave 1
- ~~**Wave 2 blocker**: starlette 1.3.1 vs the 0.3x API the plan assumed~~ — **RESOLVED.** Two real deltas found by reading the installed source: `Starlette.__init__` takes only `lifespan` (no `on_startup`/`on_shutdown`), and `uvicorn 0.51`'s `capture_signals()` nests, so `main.py` subclasses `Server` for the admin listener.
- **02-08**: `error.code` is typed `["string","null"]`, but some OpenAI-compatible relays emit an
  integer `code` (HTTP status). Under `gateway.strict_validation: true` that body fails validation on
  a relay schema whose stated purpose is never to reject forwardable traffic — same class as the
  `contentPart` defect Phase 1 found. Confirm in the fixture corpus.
- **Unclaimed**: widen `test_descriptions_make_no_absolute_containment_claim` from the 3 routing
  schemas to all 8. `CODEBASE.md` previously overclaimed that it already covered all of them.
- **Phase 11**: `secure_write` has a residual Windows window — between `os.open` and `icacls` the
  file exists with inherited ACLs. Closing it needs a security descriptor at creation (`pywin32`), a
  dependency this phase refuses. Documented, not claimed closed.
- **Phase 11**: no directory `fsync` after `os.replace` in `write_runtime`. Atomic, but not durable
  across power loss on POSIX.
- **Cleanup**: plan 02-01's Task 3 verification mints a real token into the developer's state
  directory, and 02-02's mints a real salt beside it. Intended (they prove platform behavior) but
  they are real artifacts, not fixtures.
- **02-CONTEXT.md** says `.planning/CODEBASE.md` "does not exist." It does, and it is accurate.

## Open Items — raised by Phase 2 Wave 2
- **02-07 (`doctor`)**: compare the installed shim's `PLUGIN_VERSION` against
  `hermes_auto.version.__version__` to detect a stale on-disk shim. The installed file carries the
  version that wrote it; nothing else detects drift after an upgrade.
- **`gateway.max_body_bytes` has no config key.** It is a `create_app` parameter, because
  `GatewayConfig` freezes its key set and `config.py` was forbidden to 02-04. Whoever next owns
  `config.py` should add it to `_GATEWAY_KEYS`; until then the 32 MiB default is programmatic only.
- **`_RelayResponse` is a workaround for upstream Starlette behaviour** (`StreamingResponse` never
  closes its `body_iterator`). If a future Starlette closes it, this becomes redundant — harmless,
  since closing is idempotent, but revisit at the next dependency bump.
- **`plugin.yaml` is not required for model-provider discovery** — `_import_plugin_dir` returns early
  only when `__init__.py` is missing and never reads the manifest, though every bundled plugin ships
  one. Not written. A future `hermes plugins list` surface may want it.
- `tests/performance/test_validation_cost.py` takes ~11 s, marked `performance` but not `slow`.
