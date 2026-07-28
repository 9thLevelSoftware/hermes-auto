# Hermes Auto Router — Roadmap

## Phases

- [x] Phase 1: Contracts, Schemas & Baselines (7 plans, complete)
- [x] Phase 2: Provider Plugin & Passthrough Gateway (9 plans, complete)
- [ ] Phase 3: Candidate Inventory & Model Cards (3 plans)
- [ ] Phase 4: Deterministic Routing MVP (5 plans)
- [ ] Phase 5: Cache-Aware Session Routing (4 plans)
- [ ] Phase 6: Health, Fallback & Resilient Streaming (3 plans)
- [ ] Phase 7: Adapter & Harness Expansion (4 plans)
- [ ] Phase 8: Telemetry, Outcomes & RouterBench (5 plans)
- [ ] Phase 9: Learned Capability Predictor (4 plans)
- [ ] Phase 10: Outcome Residual & Constrained Adaptation (4 plans)
- [ ] Phase 11: Production Hardening & Release (4 plans)

> Phases 1-8 deliver the first production release boundary defined in `design.md` §23. Phases 9-10 add the learned layers only after Phase 8 produces measured evidence. Phase 11 is release hardening.

## Phase Details

### Phase 1: Contracts, Schemas & Baselines
**Goal**: Freeze every public contract and establish a reproducible fixed-model baseline before any routing logic exists.
**Requirements**: R26, and the schema surfaces of R1, R6, R14, R19
**Recommended Agents**: engineering-backend-architect, engineering-security-engineer, product-technical-writer, project-manager-senior
**Success Criteria**:
- [ ] ADRs exist for provider-plus-gateway integration, session/cache switching policy, model-card architecture, local telemetry and privacy, and adapter isolation
- [ ] Versioned schemas published for the OpenAI request/response subset, the streaming SSE contract, `_hermes_auto` metadata, `RouteDecision`, model cards, and outcome events
- [ ] Supported Hermes version range is pinned and CI runs against both latest release and current `main`
- [ ] A representative fixed-model baseline corpus runs reproducibly and reports quality, cost, latency, cache, and tool-call metrics
- [ ] A written threat model covers all rows of the `design.md` §21 table
- [ ] No production routing code has been introduced
**Plans**: 7 (decomposed 2026-07-26; the original estimate of 3 did not separate the six independent contract surfaces)

### Phase 2: Provider Plugin & Passthrough Gateway
**Goal**: Prove Hermes can run entirely through the virtual provider with no observable behavioral difference against a single fixed target.
**Requirements**: R1, R2, R3, R4, R5
**Recommended Agents**: engineering-backend-architect, engineering-senior-developer, engineering-infrastructure-devops, testing-api-tester
**Success Criteria**:
- [ ] A fixed candidate produces identical behavior through the gateway and called directly
- [ ] Streaming text, single tool calls, parallel tool calls, fragmented tool-call arguments, usage reporting, and errors all match the direct path
- [ ] `hermes auto start|stop|status|doctor` work and supervise the sidecar on Windows, macOS, and Linux
- [ ] Generated bearer-token auth is enforced; the listener binds loopback only with no CORS
- [ ] CLI, TUI, gateway, desktop, and cron smoke tests pass
- [ ] Sidecar restart is safe mid-session and leaves no stale PID
- [ ] No raw prompt content appears in any log
**Plans**: 9 (decomposed 2026-07-27; the estimate of 4 predates the verified finding that the provider shim, the admin listener, and the differential harness are separate verifiable surfaces)

### Phase 3: Candidate Inventory & Model Cards
**Goal**: Build a deterministic, trustworthy candidate registry where every inclusion and exclusion is explainable.
**Requirements**: R6, R7 (compatibility subset), and the inventory surface of R22
**Recommended Agents**: engineering-backend-architect, engineering-senior-developer, testing-api-tester
**Success Criteria**:
- [ ] Model cards load and validate against JSON Schema; corrupt cards fail closed with a clear diagnostic
- [ ] Hermes/registry metadata is imported where available rather than duplicated, following the documented precedence order
- [ ] Runtime endpoint discovery and credential-availability checks populate candidate state
- [ ] Context-anchor validation fails setup when no enabled candidate supports the advertised virtual context
- [ ] Every excluded candidate carries a machine-readable reason
- [ ] Unknown prices and capabilities resolve to conservative defaults, never zero
- [ ] Each decision embeds the model-card snapshot and version
- [ ] `hermes auto models` and `hermes auto doctor` report the curated 3-6 candidate pool accurately
**Plans**: 3

### Phase 4: Deterministic Routing MVP
**Goal**: Ship genuinely useful Auto behavior with no machine learning, validated in shadow mode before it serves traffic.
**Requirements**: R7, R8, R9, R10, R11, R12, R13, R14, R20 (shadow half)
**Recommended Agents**: engineering-senior-developer, engineering-ai-engineer, testing-qa-verification-specialist, testing-performance-benchmarker
**Success Criteria**:
- [ ] Identical inputs and pinned snapshots produce byte-identical decisions
- [ ] Property tests prove a capability surplus cannot cancel another dimension's deficit
- [ ] No hard filter can be overridden by any scoring component
- [ ] `quality`, `balanced`, and `economy` produce measurably different candidate distributions on the baseline corpus
- [ ] Expected-cost estimates reconcile against actual provider usage within a documented tolerance
- [ ] Pareto-dominated candidates are pruned before final ranking
- [ ] Explanations render with correct reason codes for every decision path
- [ ] Shadow mode reports expected cost and candidate distribution while the fixed candidate still serves the request
- [ ] Deterministic router p99 stays under 50 ms
**Plans**: 5

### Phase 5: Cache-Aware Session Routing
**Goal**: Make routing safe for long-running agent trajectories by routing on cache boundaries rather than on every model call.
**Requirements**: R15, R16, R17
**Recommended Agents**: engineering-backend-architect, engineering-senior-developer, testing-qa-verification-specialist
**Success Criteria**:
- [ ] Zero unexpected model switches occur inside a locked tool loop
- [ ] Lane derivation separates parent agent, delegate subagents, compression, vision, web extraction, and title generation
- [ ] Subagents route independently while preserving the root session relationship
- [ ] Context compression and prefix discontinuity open a new cache epoch and permit rerouting
- [ ] Hysteresis retains the current candidate unless the loss gap exceeds the mode threshold plus switch penalty
- [ ] Pin, unpin, reroute, and `auto:session` behave per the switching-policy matrix
- [ ] Cache-preserving decisions are visible in explanations via `STICKY_CACHE` / `SWITCH_HYSTERESIS`
- [ ] Concurrent Hermes sessions against one sidecar cannot overwrite one another's routes
**Plans**: 4

### Phase 6: Health, Fallback & Resilient Streaming
**Goal**: Make operational conditions part of safe selection without letting health reshuffle the semantic ranking.
**Requirements**: R18, R11 (health half)
**Recommended Agents**: engineering-infrastructure-devops, engineering-backend-architect, testing-api-tester
**Success Criteria**:
- [ ] Failures before the first streamed event fall back to the next healthy ranked candidate at least 99% of the time under injected faults
- [ ] Failures after stream commitment never splice output from a second model — the request fails cleanly
- [ ] The health veto removes unavailable candidates without reordering healthy ones
- [ ] A rate-limited credential does not poison the entire provider
- [ ] Circuit breakers maintain separate provider, endpoint, model, credential, and candidate/harness states with half-open probes
- [ ] Hard budget, local-only policy, denylist, context, and tool requirements remain enforced throughout every fallback path
- [ ] The full `design.md` §20.6 fault-injection matrix passes
**Plans**: 3

### Phase 7: Adapter & Harness Expansion
**Goal**: Move beyond the single lowest-risk OpenAI-compatible path without leaking incompatible fields across providers.
**Requirements**: R22
**Recommended Agents**: engineering-backend-architect, engineering-senior-developer, testing-api-tester
**Success Criteria**:
- [ ] The `BackendAdapter` interface is stable and every adapter passes the identical contract suite
- [ ] Candidate identity includes harness version, and harness profiles are versioned artifacts
- [ ] Optional LiteLLM transport works without owning any routing decision
- [ ] At least one direct native adapter normalizes system roles, tool identifiers, tool results, images, reasoning controls, and cached-token usage
- [ ] Protocol-crossing transcript tests prove cross-provider histories do not leak incompatible or signed fields
- [ ] The optional Hermes-native credential bridge degrades to supported adapters on version mismatch rather than corrupting sessions
**Plans**: 4

### Phase 8: Telemetry, Outcomes & RouterBench
**Goal**: Produce the local evidence needed to justify routing decisions and to train anything later.
**Requirements**: R19, R20, R21
**Recommended Agents**: data-analytics-engineer, engineering-backend-architect, engineering-security-engineer, testing-test-results-analyzer
**Success Criteria**:
- [ ] A complete route is reproducible from pinned snapshots with 100% deterministic replay
- [ ] Actual costs reconcile against provider-reported usage
- [ ] All nine required baselines run, including the oracle hindsight selector, and produce frontier reports
- [ ] Router benefit is reported per successful task, not merely per request
- [ ] Reports separate model quality from provider reliability
- [ ] Retention and deletion controls work; raw prompt retention measures 0 by default
- [ ] External export remains disabled by default and requires explicit consent
- [ ] Quality non-inferiority gates are defined against a selected fixed baseline
**Plans**: 5

### Phase 9: Learned Capability Predictor
**Goal**: Replace brittle heuristics with a learned requirement predictor while leaving the deterministic matcher fully in control.
**Requirements**: R23
**Recommended Agents**: engineering-ai-engineer, data-analytics-engineer, testing-performance-benchmarker
**Success Criteria**:
- [ ] The predictor outputs capability requirements, never target model IDs
- [ ] Learned routing regret is lower than heuristic routing on held-out data
- [ ] Classifier p99 on CPU stays under 100 ms and within the overall router latency gate
- [ ] Language and domain slices are evaluated separately, not only in aggregate
- [ ] Low-confidence and out-of-distribution inputs fall back to the deterministic heuristic within the timeout
- [ ] Adding a new model requires only a new model card, with no predictor retraining
- [ ] The training corpus is opt-in and contains no raw retained prompts beyond what the user approved
**Plans**: 4

### Phase 10: Outcome Residual & Constrained Adaptation
**Goal**: Add the Cursor-like behavioral layer as a bounded residual that can reorder safe candidates but never cross a hard boundary.
**Requirements**: R24
**Recommended Agents**: engineering-ai-engineer, project-management-experiment-tracker, testing-test-results-analyzer
**Success Criteria**:
- [ ] The residual measurably improves outcomes over deterministic routing in offline evaluation
- [ ] The residual is provably clamped and cannot override hard filters, mode shortfall ceilings, budget caps, user pins, or health vetoes
- [ ] Cold-start candidates default to residual 0, preserving the deterministic baseline
- [ ] Exploration is opt-in, never runs on high-risk tasks, logs selection propensity, and maintains a fixed holdout group
- [ ] Automatic rollback triggers on quality, cost, tool-call, or latency regression
- [ ] Drift detection flags model and provider version changes
- [ ] The kill switch returns users to deterministic routing immediately
**Plans**: 4

### Phase 11: Production Hardening & Release
**Goal**: Make the distribution installable, upgradable, and maintainable outside a development checkout.
**Requirements**: R25
**Recommended Agents**: engineering-infrastructure-devops, engineering-security-engineer, product-technical-writer, project-manager-senior
**Success Criteria**:
- [ ] Clean installation and uninstall on Windows, macOS, and Linux, with service installation for Windows services, launchd, and systemd
- [ ] Zero Hermes core files modified, verified by a repository check
- [ ] Schema migrations, configuration backup, and rollback tests pass
- [ ] Startup repair handles stale PIDs and orphaned sockets
- [ ] The sidecar survives extended concurrent soak and load testing without memory growth
- [ ] Dependency and secret scanning pass; the security and privacy checklist passes
- [ ] Installation, privacy, model-onboarding, troubleshooting, and evaluation-methodology guides are published
- [ ] Nightly compatibility CI against Hermes `main` is green
- [ ] Release metrics satisfy the selected rollout gates, including at least 20% `balanced` cost reduction with no significant quality regression
**Plans**: 4

## Progress

| Phase | Plans | Completed | Status |
|-------|-------|-----------|--------|
| 1. Contracts, Schemas & Baselines | 7 | 7 | Complete (review passed) |
| 2. Provider Plugin & Passthrough Gateway | 9 | 9 | Complete (review passed, 2 cycles) |
| 3. Candidate Inventory & Model Cards | 3 | 0 | Not started |
| 4. Deterministic Routing MVP | 5 | 0 | Not started |
| 5. Cache-Aware Session Routing | 4 | 0 | Not started |
| 6. Health, Fallback & Resilient Streaming | 3 | 0 | Not started |
| 7. Adapter & Harness Expansion | 4 | 0 | Not started |
| 8. Telemetry, Outcomes & RouterBench | 5 | 0 | Not started |
| 9. Learned Capability Predictor | 4 | 0 | Not started |
| 10. Outcome Residual & Constrained Adaptation | 4 | 0 | Not started |
| 11. Production Hardening & Release | 4 | 0 | Not started |
| **Total** | **52** | **16** | **Phases 1-2 complete and reviewed** |
