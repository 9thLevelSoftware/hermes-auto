# Changelog

All notable changes are documented here. The project follows Semantic
Versioning and the Keep a Changelog format.

## [Unreleased]

## [0.2.0] - 2026-07-30

### Added

- Literal `/model auto` Hermes alias and `auto`-first model listing.
- Ordered, user-approved OpenAI-compatible candidate configuration.
- Read-only Hermes model discovery with interactive, confirmed configuration.
- Deterministic capability/context eligibility and complexity-tier selection.
- Turn-level tool-loop stickiness and `auto:session` pinning in bounded memory.
- Per-candidate upstream client pool with retryable normal and streaming fallback.
- First-byte streaming commit barrier preventing mixed-model responses.
- Authenticated bounded decision explanations through the admin API, CLI, and
  `/auto`.
- Dependency-free home-scoped control plugin delegating to the standalone
  executable.

### Changed

- `hermes-auto setup` now installs both home-scoped Hermes plugins, enables the
  control plugin, and preserves unrelated configuration while adding the Auto
  alias.
- Outbound requests rewrite the virtual model to the selected real model and
  strip private metadata while preserving all other fields.
- Existing fixed `auto_router.upstream` configuration migrates to one balanced
  candidate with a warning.
- Active product documentation now describes the focused deterministic router.

### Removed

- Active evaluation, benchmark, learned-routing, adapter, model-card,
  outcome-event, replay, and persistent-telemetry scaffolding.
- Admin mutation and feedback placeholders.

### Preserved

- Loopback authentication, provider shim, raw SSE relay, supervision, health
  checks, and the Windows no-visible-terminal process-launch fix.

The original research design and planning history remain under
`docs/research-archive/`.

## [0.1.0]

- Initial provider shim, authenticated local passthrough gateway, supervision,
  raw streaming relay, setup tooling, compatibility checks, and research
  contracts.
