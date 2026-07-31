# Hermes Auto Router

## What This Is
An out-of-tree Python distribution that gives Hermes Agent a capability-, cost-, cache-, and outcome-aware "Auto" model. It ships three cooperating pieces: a `hermes-auto` model-provider plugin, a `hermes-auto-control` control plugin, and a local OpenAI-compatible routing gateway that runs as a supervised sidecar.

Hermes continues to believe it is talking to one stable provider and one virtual model (`auto:balanced`). The gateway selects the real model and provider behind that boundary, then normalizes the response.

## Core Value
Combines GitHub HyDRA's safety architecture (predict model-independent capability requirements, match against external model cards, pick the cheapest sufficiently capable candidate, keep the learned layer subordinate to deterministic policy) with Cursor Router's practical agent concerns (selectable cost-quality modes, domain affinity, cache-aware stickiness, model-specific harnesses, outcome-driven improvement).

Critically, it does this at the **supported provider boundary** rather than inside `AIAgent` — no Hermes core modification, no private runtime mutation, no fork maintenance, and no conflict with upstream policy, which has explicitly rejected plugin-controlled per-call model overrides.

## Who It's For
- Hermes Agent users running multi-model setups who currently pin one fixed model and overpay or under-serve depending on the task.
- Developers with mixed local (Ollama, vLLM, LM Studio) and hosted (OpenRouter, direct provider) inventories who want privacy-aware routing.
- Operators who need hard policy guarantees — budget ceilings, provider allowlists, local-only data residency — that a routing layer must never silently violate.
- Teams evaluating cost per *completed task* rather than cost per request.

## Requirements

### Validated
(None yet — ship to validate)

### Active
- [ ] R1: `hermes-auto` ProviderProfile registering virtual models `auto:quality`, `auto:balanced`, `auto:economy`, `auto:session` with `_hermes_auto` routing metadata via `build_extra_body()`
- [ ] R2: `hermes-auto-control` plugin — slash commands (`/auto status|mode|explain|candidates|pin|unpin|reroute|feedback|stats`), CLI subcommands (`hermes auto setup|start|stop|restart|status|doctor|models|benchmark|explain|export-diagnostics`), lifecycle hooks, sidecar supervision
- [ ] R3: Local routing gateway exposing OpenAI-compatible `/v1/chat/completions` and `/v1/models`, plus `/healthz`, `/readyz`, and a separately-scoped admin API
- [ ] R4: Loopback binding, generated bearer-token auth, restrictive token-file permissions, and process supervision on Windows, macOS, and Linux
- [ ] R5: Streaming SSE and tool-call behavior identical to talking to the target provider directly
- [ ] R6: Model-card schema with JSON Schema validation, runtime endpoint discovery, credential checks, capability/domain overrides, and context-anchor validation
- [ ] R7: Hard eligibility filters (13 conditions) that no learned component can ever restore a candidate past
- [ ] R8: Deterministic feature extraction and heuristic requirement prediction producing an 8-dimension capability vector plus domain tags and policy classifications
- [ ] R9: Positive capability-shortfall scoring — surplus in one dimension cannot conceal a deficit in another
- [ ] R10: Expected-cost model covering uncached input, cached reads, cache writes, model-specific output-token prediction, and fixed request fees; unknown pricing never treated as zero
- [ ] R11: Rolling latency and reliability statistics keyed by `(provider, model, endpoint, harness_version)`
- [ ] R12: Domain affinity scoring, Pareto pruning, and the final route-loss minimization
- [ ] R13: `quality`, `balanced`, `economy`, and `session` modes with mode-specific thresholds and dimension weights
- [ ] R14: `RouteDecision` records, reason codes, and user-facing explanations derived only from router inputs — never from hidden model reasoning
- [ ] R15: Four-part route identity — root session, lane, user turn, cache epoch — with prefix-discontinuity detection
- [ ] R16: Tool-loop route locking, switch-cost estimation, and hysteresis so a route changes only at safe boundaries
- [ ] R17: Pin, unpin, reroute, session-only mode, and auxiliary virtual aliases (`auto:compression`, `auto:vision`)
- [ ] R18: Health tracking, multi-level circuit breakers, pre-stream fallback, and a first-chunk commit barrier that forbids post-commit model splicing
- [ ] R19: Local SQLite (WAL) telemetry with salted session hashing, retention and deletion controls, explicit feedback capture, and zero raw prompt/tool-result/secret retention
- [ ] R20: Shadow routing plus a static replay harness producing deterministic decisions from pinned snapshots
- [ ] R21: Dynamic execution evaluation track, all nine required baselines, and cost-quality-latency frontier reports
- [ ] R22: `BackendAdapter` interface with OpenAI-compatible, OpenRouter, and local adapters; optional LiteLLM transport; versioned harness profiles; optional fail-closed Hermes-native credential bridge
- [ ] R23: Learned capability-requirement predictor (compact encoder, ONNX, quantized, strict timeout, heuristic fallback) that predicts capabilities rather than model IDs
- [ ] R24: Bounded outcome residual, constrained contextual bandit with opt-in exploration, automatic regression rollback, and an immediate kill switch
- [ ] R25: Packaging, isolated sidecar environment, schema migrations, service installation (Windows/launchd/systemd), docs, and nightly compatibility CI against Hermes `main`
- [ ] R26: Frozen public contracts before routing logic — ADRs, versioned schemas, threat model, and a reproducible fixed-model baseline corpus

### Out of Scope
- Mutating `AIAgent` or any Hermes core file from the plugin
- A fork-native `ModelRouteProvider` SPI (kept as a separate optional track, not this project)
- Online learning and automatic exploration in the first production release
- Unbounded model catalogs — curation beats catalog size
- Mid-tool-loop model switching
- Post-stream model splicing after the first chunk is committed
- Fully model-specific tool sets that expose different tool names to different models
- Required dependence on private Hermes authentication internals (the credential bridge stays optional and fail-closed)
- Automatic external telemetry export
- A remote, multi-tenant hosted routing service
- Routing decisions driven by one LLM judging another live request

## Constraints
- No Hermes core file modifications; the supported provider boundary is the only integration seam
- Upstream Hermes policy rejects plugin-controlled per-call model/provider overrides — a proposed failover-routing hook was closed unmerged
- Prompt-cache preservation is a hard invariant; cache-breaking model changes are treated as defects
- Requires a supervised sidecar process across Windows, macOS, and Linux
- Loopback-only binding, generated bearer token, no CORS
- No raw prompts, tool-result bodies, or secrets stored by default; external export is opt-in per Hermes contribution policy
- Performance gates: deterministic router p99 under 50 ms; learned classifier p99 under 100 ms on CPU; added TTFT overhead under 1% or 100 ms p99
- Zero tolerance gates: unexpected model changes within a tool loop, hard-policy violations, and default raw-prompt retention must all be 0
- Behavioral settings live in `config.yaml`; only credentials and generated local tokens belong in environment variables
- Sidecar dependencies must be isolated to avoid conflicts with the Hermes environment
- Supported Hermes version range must be pinned, with CI against both latest release and current `main`

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Integration boundary | Upstream rejects per-call plugin routing; the provider boundary is the only supported seam and avoids private runtime mutation and fork maintenance | Custom provider plugin + local OpenAI-compatible gateway sidecar |
| Learning sequence | Large-scale router evaluation found sophisticated routers often fail to beat simple baselines under unified evaluation; pool curation matters more than pool size | Deterministic-first; ML introduced only after Phase 8 produces a measured baseline |
| Capability model | Prevents a surplus in one dimension from masking a deficit in another | 8-dimension positive capability shortfall with mode-configurable weights |
| Switching policy | Cache misses and harness transitions dominate the real cost of switching mid-conversation | Session/lane/turn/cache-epoch identity plus hysteresis and a first-chunk commit barrier |
| Adapter rollout order | Lowest cross-provider replay risk and fastest path to a working MVP | Tier 1 OpenAI-compatible/aggregator, Tier 2 LiteLLM transport, Tier 3 native adapters, Tier 4 optional Hermes credential bridge |
| LiteLLM role | The router must own policy; LiteLLM normalizes transport across 100+ providers but is not a decision engine | Execution transport only, run in the isolated sidecar environment |
| Telemetry posture | Hermes contribution policy requires outbound telemetry and attribution to be gated behind explicit opt-in | Local SQLite store, no raw prompt retention, external export disabled by default |
| Fork-native track | Permanent fork divergence and coupling to every Hermes call site are not yet justified | Deferred; the standalone gateway becomes the reference implementation and evaluation oracle first |
| Candidate pool size | Larger pools create diminishing returns and model-recall failures | Start with 3-6 curated candidates |
| Design source | `design.md` is a complete specification covering product, architecture, config, repo layout, phasing, tests, and threat model | Project initialized from `design.md`; requirements and phases traced to it |
| Execution mode | High-stakes routing layer with hard policy invariants warrants approval before each step | Guided |
| Planning depth | Complex routing and ML domain; mirrors the design document's own Phase 0-10 staging | Deep Analysis (11 phases) |
| Cost profile | Correctness of hard-policy invariants and contract design outweighs execution cost | Premium (Opus for planning and execution, Sonnet for checks) |

## Architecture Influences
**Language/runtime**: Python, distributed via pip as `hermes-auto-router` with `src/hermes_auto/` layout. Sidecar dependencies isolated from the Hermes environment.

**Shape**: Thin declarative `ProviderProfile` in-process with Hermes; all operational routing in the gateway sidecar. Gateway modules split into Ingress, Canonicalization, State, Routing, Execution, and Evidence.

**Inference API**: OpenAI Chat Completions, because Hermes's normal chat transport already uses OpenAI-formatted messages and tools for many providers. Admin API on a separate auth scope and preferably a separate local listener.

**Storage**: SQLite in WAL mode for local telemetry — route decisions, attempts, usage, health observations, turn/session outcomes, model-card versions, and feedback.

**Model metadata precedence**: hard operator policy > live health > measured candidate statistics > curated model card > registry metadata > conservative unknown defaults. Reuse Hermes's existing offline-first model information rather than duplicating it.

**Virtual context**: Hermes needs a declared `context_length` (256000) for its own preflight and compression decisions, so setup and doctor must validate that at least one enabled context-anchor candidate actually supports the advertised window.

**Research basis**: GitHub HyDRA (capability prediction decoupled from model catalog), Cursor Router (modes, domain affinity, cache accounting, harness adaptation), TwinRouterBench (agentic trajectory evaluation rather than one-shot prompts), and current routing/serving literature as of 2026-07-26.

---
*Last updated: 2026-07-26 after initialization*
