# Project State

## Current Position
- **Phase**: 1 of 11 (planned)
- **Status**: Phase 1 planned — 7 plans across 3 waves, critique applied
- **Last Activity**: Phase 1 planning (2026-07-26)

## Progress
```
[····················] 0% — 0/47 plans complete
```

## GitHub
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

## Open Items
- Decide in Phase 7 whether to mirror Hermes's `requires-python = ">=3.11,<3.14"` upper bound in this project when the optional Tier-4 credential bridge lands

## Resolved
- ~~Provision `HERMES_DIST_NAME` and `HERMES_GIT_URL`~~ — both set 2026-07-27; the nightly workflow will genuinely measure against PyPI `hermes-agent` and `NousResearch/hermes-agent@main`
- ~~Pin the `design.md` revision~~ — blob `18bb54b36485fa0813ec67f84a74628a9eee3aae` at commit `331a69d`, recorded in `01-CONTEXT.md` with a verification command

## Next Action
Run `/legion:build` to execute Phase 1: Contracts, Schemas & Baselines
