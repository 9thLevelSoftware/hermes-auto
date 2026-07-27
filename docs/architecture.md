# Hermes Auto Router — Architecture Overview

This document is the entry point to the system's architecture. It describes the shape of the system
and the pieces that ship, then indexes the [architecture decision records](adr/README.md) that hold
the authoritative reasoning.

**Where authority lives.** `design.md` is the long-form design rationale and remains read-only. The
ADRs are the citable, individually revisable decision records extracted from it. **This overview
summarizes; the ADRs decide.** Where a summary here and an ADR appear to disagree, the ADR governs
and the discrepancy is a bug in this file.

---

## System shape

Hermes is configured to use one stable provider and one virtual model. It does not know that routing
is happening. The gateway selects the real model and provider behind that boundary, then normalizes
the response back into the shape Hermes expects (`design.md §1`).

```text
Hermes CLI / TUI / Gateway / Desktop / Cron / Subagents
                         │
                         ▼
                 Hermes AIAgent
                         │
                         ▼
           ProviderProfile: hermes-auto
           Virtual model: auto:balanced
                         │
                  localhost HTTPS/HTTP
                         │
                         ▼
              Hermes Auto Router gateway
 ┌─────────────────────────────────────────────────────────────┐
 │ Request canonicalization                                    │
 │ Session, lane, turn, and cache-epoch tracking               │
 │ Hard policy and compatibility filters                       │
 │ Capability requirement prediction                           │
 │ Model-card matching and expected-cost calculation           │
 │ Cache-aware stickiness and switch hysteresis                │
 │ Provider/model health veto                                  │
 │ Model-specific request and response adapters                │
 │ Local telemetry, outcomes, replay, and explanations         │
 └─────────────────────────────────────────────────────────────┘
           │                 │                 │
           ▼                 ▼                 ▼
    Local models       Aggregators       Direct providers
 Ollama/vLLM/etc.      OpenRouter/etc.   OpenAI/Anthropic/etc.
```

### Internal gateway modules

The gateway is organized into six module groups (`design.md §5.4`):

```text
Ingress
  ├── authentication
  ├── request-size and token checks
  ├── OpenAI schema validation
  └── removal of internal routing metadata

Canonicalization
  ├── message normalization
  ├── tool-schema normalization
  ├── modality extraction
  └── token and context estimation

State
  ├── root session
  ├── lane
  ├── user turn
  ├── tool-loop lock
  ├── cache epoch
  ├── pinned candidate
  └── active route

Routing
  ├── hard eligibility filters
  ├── requirement predictor
  ├── domain classifier
  ├── model-card matcher
  ├── expected cost and latency
  ├── capability shortfall
  ├── bounded learned residual
  ├── switch hysteresis
  └── health veto

Execution
  ├── target adapter
  ├── request/harness profile
  ├── streaming commit barrier
  ├── retry and fallback
  └── normalized response

Evidence
  ├── decision explanations
  ├── usage and cost
  ├── health observations
  ├── outcome events
  ├── replay corpus
  └── optional exporters
```

Each group maps onto a decision record: **State** implements the identity model of
[ADR-0002](adr/0002-session-and-cache-switching-policy.md), **Routing** consumes the model cards of
[ADR-0003](adr/0003-model-card-architecture.md), **Execution** uses the adapter tiers of
[ADR-0005](adr/0005-adapter-isolation-and-rollout-tiers.md), and **Evidence** is bound by
[ADR-0004](adr/0004-local-telemetry-and-privacy.md).

---

## Components

The distribution ships as one product, `hermes-auto-router`, containing three cooperating pieces
(`design.md §1`, `design.md §5`).

### 1. `hermes-auto` — the model-provider plugin

A **thin, declarative** `ProviderProfile` registered in-process with Hermes. It advertises the
virtual models `auto:quality`, `auto:balanced`, `auto:economy`, and `auto:session`, points its
`base_url` at the loopback gateway, and injects routing metadata through the supported
`build_extra_body()` hook under a `_hermes_auto` key — protocol version, root session identifier,
virtual model, and plugin version. The gateway strips that key before sending anything upstream
(`design.md §5.1`).

It contains **no operational routing logic**. Hermes provider profiles are intentionally separate
from client construction, credential rotation, and streaming, which is what makes the gateway the
correct home for the router. See
[ADR-0001](adr/0001-provider-plugin-and-local-gateway.md).

### 2. `hermes-auto-control` — the control plugin

An opt-in second module in the same distribution, enabled through `plugins.enabled`. It registers
CLI commands, slash commands, session lifecycle hooks, post-turn and post-tool outcome reporters,
sidecar process supervision, local health checks, and decision correlation (`design.md §5.2`).

Its hooks are **observational and administrative only**. It must never alter the chosen provider or
model inside `AIAgent` — that prohibition is the load-bearing part of
[ADR-0001](adr/0001-provider-plugin-and-local-gateway.md).

### 3. The local routing gateway

A supervised sidecar process, bound to loopback, running in its **own isolated Python environment**.
It exposes an OpenAI Chat Completions-compatible inference API plus health endpoints, and a
separately scoped administrative API — preferably on a separate local listener (`design.md §5.3`):

```text
POST /v1/chat/completions
GET  /v1/models

GET  /healthz
GET  /readyz

GET  /admin/v1/status
GET  /admin/v1/decisions/{decision_id}
POST /admin/v1/sessions/{session_id}/reroute
POST /admin/v1/sessions/{session_id}/pin
POST /admin/v1/feedback
```

This is where all operational routing lives: state tracking, eligibility filtering, candidate
scoring, adapter execution, and evidence collection.

---

## Decision records

The records live in `docs/adr/`. The full index, template, and numbering convention are in
[`adr/README.md`](adr/README.md). Repo-relative paths, for citation from plans and code comments:

- `docs/adr/0001-provider-plugin-and-local-gateway.md`
- `docs/adr/0002-session-and-cache-switching-policy.md`
- `docs/adr/0003-model-card-architecture.md`
- `docs/adr/0004-local-telemetry-and-privacy.md`
- `docs/adr/0005-adapter-isolation-and-rollout-tiers.md`

| ADR | Title | Summary |
|-----|-------|---------|
| [0001](adr/0001-provider-plugin-and-local-gateway.md) | Route behind the Hermes provider boundary, not inside `AIAgent` | The supported provider boundary is the only integration seam; a local OpenAI-compatible gateway sidecar does the selecting, because upstream policy rejects plugin-controlled per-call model overrides. |
| [0002](adr/0002-session-and-cache-switching-policy.md) | Route on cache boundaries using a four-part route identity | Root session, lane, user turn, and cache epoch define when a route may change; routes lock across tool loops and a first-chunk commit barrier forbids splicing a second model into a committed stream. |
| [0003](adr/0003-model-card-architecture.md) | Candidates are identity tuples described by versioned model cards | A candidate is (provider, endpoint, model, credential scope, protocol adapter, harness profile, harness version), described by a versioned card, with a six-level metadata precedence chain topped by hard operator policy. |
| [0004](adr/0004-local-telemetry-and-privacy.md) | Local-first telemetry with no raw prompt retention | A local SQLite WAL store holding derived features only — no raw prompts, no tool-result bodies, no secrets — with salted session hashing and opt-in external export. |
| [0005](adr/0005-adapter-isolation-and-rollout-tiers.md) | Four-tier adapter rollout with an isolated sidecar environment | Tier 1 OpenAI-compatible/local, Tier 2 LiteLLM as transport only, Tier 3 native adapters after contract stability, Tier 4 an optional fail-closed Hermes credential bridge — all in an isolated environment. |

### What the ADRs deliberately do not fix

Routing algorithm internals — scoring formulas, dimension weight values, hysteresis thresholds, and
circuit-breaker constants — are **not** frozen by these records. They are implementation choices
owned by the routing phases, tuned against measurement. The ADRs fix the shape of the system: where
it integrates, what a candidate is, when a route may change, what may be stored, and how requests
are executed.

---

## Related documentation

- [`adr/README.md`](adr/README.md) — ADR template, numbering convention, and index.
- [`threat-model.md`](threat-model.md) — security and privacy threat model mapping each identified
  threat to the module that mitigates it. *Created in a later plan of this phase.*
- [`privacy.md`](privacy.md) — the user-facing privacy guide, derived from
  [ADR-0004](adr/0004-local-telemetry-and-privacy.md). *Created in a later plan of this phase.*
- [`evaluation.md`](evaluation.md) — the baseline corpus, measurement harness, and evaluation
  program. *Created in a later plan of this phase.*

The last three links are **intentional forward references**. They point at documents owned by later
plans in this phase and are expected to be unresolved until those plans land.
