# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository skeleton and packaging contract
- Five architecture decision records covering provider-plugin and local-gateway integration, session
  and cache switching policy, model-card architecture, local-first telemetry and privacy, and
  adapter isolation with rollout tiers
- Seven versioned JSON Schemas: the OpenAI chat request and response subsets, the SSE stream
  contract, the `_hermes_auto` metadata envelope, `RouteDecision`, model cards, and outcome events
- Schema loader `hermes_auto.gateway.schemas` with `$id`-keyed lookup, duplicate-`$id` detection, and
  a `validate` helper
- Fail-closed Hermes compatibility probe `hermes_auto.compatibility`, pinning the supported Hermes
  range to `>=0.19,<1.0`
- Two GitHub Actions workflows: a cross-platform test matrix with a schema-validation phase-close
  gate and a `package` job that asserts the built wheel carries all seven schemas plus `py.typed`
  and then installs it into a clean virtualenv outside the repository and loads them from there —
  the only gate that inspects the shipped artifact rather than the source tree — and nightly
  compatibility runs against the latest Hermes release and `main`
- STRIDE threat model covering all fourteen `design.md` §21 rows, with trust boundaries, assets,
  residual risk, and a per-phase review cadence
- User-facing privacy and telemetry guide documenting the four storage prohibitions and per-phase
  control availability
- Evaluation methodology: nine baseline strategies, the reproducibility contract, metric
  definitions, and the current limits of what is measurable
- Fixed-model baseline corpus of eleven tasks and the `hermes_auto.evaluation` metric harness
- `scripts/benchmark.py`, an offline baseline report aggregator that validates every recorded event
  against `outcome-event.v1` and produces byte-identical output for identical inputs
