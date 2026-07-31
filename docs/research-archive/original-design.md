# Hermes Auto Router

## Design, development, and implementation plan

**Research basis:** GitHub Copilot Auto, Cursor Router, current Hermes Agent `main` at commit `0054c7e…`, and current routing research as of **July 26, 2026**.

---

## 1. Executive recommendation

Build **Hermes Auto Router** as an out-of-tree Python distribution containing three cooperating pieces:

1. **A Hermes model-provider plugin** named `hermes-auto`.
2. **A lightweight Hermes control plugin** for setup, status, explanations, pinning, feedback, and lifecycle management.
3. **A local OpenAI-compatible routing gateway** that receives Hermes requests, selects the real model, invokes it, and normalizes the response.

Hermes would continue to believe it is talking to one stable provider and one virtual model:

```yaml
model:
  provider: hermes-auto
  model: auto:balanced
  context_length: 256000
```

The routing gateway would then select the actual model and provider behind that stable interface.

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

### Why this architecture is necessary

Hermes deliberately treats prompt caching as a core invariant and prefers capabilities to live at plugin and provider edges rather than expanding the central agent loop. Its development guidance explicitly warns against cache-breaking model changes and plugins that modify core files.

Hermes supports:

* Pip-distributed plugins.
* Slash commands and CLI subcommands.
* Model-provider plugins.
* Provider-specific request metadata.
* Session identifiers in provider requests.
* A shared inference path across Hermes surfaces.

However, current Hermes policy rejects plugin-controlled, per-call model/provider overrides inside `AIAgent`. A proposed failover-routing hook was closed unmerged, with the maintainers explicitly directing users toward configured providers and fallback chains rather than plugin-selected live routing.

Therefore:

> **The supported provider boundary should be the integration point. The router belongs behind that boundary, not inside `AIAgent`.**

This remains a genuine Hermes plugin while avoiding private runtime mutation, upstream policy conflict, and fork maintenance.

---

# 2. What “best of both worlds” should mean

GitHub’s HyDRA design separates request understanding from model selection. A learned model predicts four capability requirements, while a deterministic algorithm compares those requirements against external model profiles and selects the cheapest sufficiently capable model. This keeps the learned model independent of the changing model catalog. ([arXiv][1])

Cursor Router adds contextual task classification, online user-outcome signals, selectable cost–quality modes, provider neutrality, and model-specific harness behavior. Cursor also explicitly accounts for the cache misses and harness-transition problems caused by switching models during a conversation. ([Cursor][2])

The Hermes implementation should combine them as follows:

| Source concept                                           | Hermes Auto Router adaptation                                                                       |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| GitHub: predict capability requirements, not model names | Predict a model-independent requirement vector                                                      |
| GitHub: external model capability profiles               | Versioned model cards combining static metadata, evaluations, and measured performance              |
| GitHub: positive capability shortfall                    | A model’s surplus in one capability cannot conceal a deficit in another                             |
| GitHub: cheapest sufficiently capable model              | Deterministic quality floor before cost optimization                                                |
| GitHub: independent health veto                          | Rank semantically first, then eliminate unhealthy candidates                                        |
| GitHub: route on natural cache boundaries                | Session and cache-epoch stickiness, not free switching on every agent step                          |
| Cursor: task domain and behavioral affinity              | Domain tags and per-model affinity scores                                                           |
| Cursor: selectable Pareto positions                      | `quality`, `balanced`, and `economy` modes                                                          |
| Cursor: user-satisfaction optimization                   | A bounded learned residual trained from Hermes outcomes                                             |
| Cursor: model-specific harnesses                         | Versioned request, tool-schema, reasoning, cache, and response adapters                             |
| Cursor: online experiments                               | Shadow routing, replay, opt-in A/B tests, then constrained exploration                              |
| Cursor: cost per completed work                          | Cost per successful turn, passed task, accepted edit, or completed goal—not merely cost per request |

The learned component must remain subordinate to deterministic policy. It may improve ranking among safe candidates, but it must never override:

* Context-window requirements.
* Tool or modality compatibility.
* Privacy or local-only policy.
* Provider allowlists.
* Hard cost limits.
* Health circuit breakers.
* User model pins.

That is the primary safety advantage inherited from HyDRA.

---

# 3. Approaches considered

| Approach                                               | Advantages                                                                                                  | Problems                                                                                                              | Decision                 |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| Mutate `AIAgent` from a normal plugin                  | Lowest routing latency; direct access to Hermes clients                                                     | No supported route-replacement API; private-field coupling; conflicts with current upstream policy; likely cache bugs | Reject                   |
| Add a core route-selector hook                         | Native credentials and task metadata                                                                        | Current maintainers have rejected this direction; requires fork maintenance                                           | Optional fork-only track |
| Use delegation alone                                   | Supported by Hermes; fresh context is cache-friendly                                                        | Does not select the parent conversation model and cannot transparently implement Auto                                 | Complementary only       |
| Register a custom provider pointing at a local gateway | No Hermes core modifications; stable OpenAI interface; works across surfaces; isolates routing dependencies | Requires a supervised sidecar process                                                                                 | **Recommended**          |
| Use a remote routing gateway                           | Centralized team policy and telemetry                                                                       | Privacy, latency, deployment, and service-operability burden                                                          | Later enterprise mode    |

---

# 4. Product definition

## 4.1 User-facing behavior

The plugin should expose these virtual models:

```text
auto:quality
auto:balanced
auto:economy
auto:session
```

Recommended semantics:

| Mode            | Behavior                                                                                          |
| --------------- | ------------------------------------------------------------------------------------------------- |
| `auto:quality`  | Strict capability floor; cost is secondary; allows escalation to frontier models                  |
| `auto:balanced` | Default; preserves a strong quality floor while optimizing cost and latency                       |
| `auto:economy`  | Aggressive cost optimization within non-negotiable compatibility, privacy, and budget constraints |
| `auto:session`  | Select once and remain pinned until session reset, compression, hard failure, or explicit reroute |

Optional aliases can mirror Cursor terminology:

```text
auto:intelligence -> auto:quality
auto:cost         -> auto:economy
```

The actual selected model should be transparent through:

* `/auto explain`
* `/auto status`
* `hermes auto explain --last`
* Response metadata and local decision logs
* Optional display beside each Hermes response

The underlying target should **not** be hidden by default.

## 4.2 Explicit user controls

```text
/auto status
/auto mode quality|balanced|economy|session
/auto explain
/auto candidates
/auto pin <candidate-id>
/auto unpin
/auto reroute
/auto feedback good|bad
/auto stats
```

CLI equivalents:

```text
hermes auto setup
hermes auto start
hermes auto stop
hermes auto restart
hermes auto status
hermes auto doctor
hermes auto models
hermes auto benchmark
hermes auto explain --last
hermes auto export-diagnostics
```

These commands are a natural fit for Hermes because plugins can register both slash commands and top-level CLI subcommands without introducing a new model-visible tool.

---

# 5. Target component architecture

## 5.1 Thin model-provider plugin

The provider module should register a `ProviderProfile` similar to:

```python
class HermesAutoProfile(ProviderProfile):
    name = "hermes-auto"
    display_name = "Hermes Auto Router"
    description = "Capability-, cost-, cache-, and outcome-aware model routing"
    api_mode = "chat_completions"
    base_url = "http://127.0.0.1:8787/v1"
    env_vars = ("HERMES_AUTO_ROUTER_TOKEN",)
    fallback_models = (
        "auto:balanced",
        "auto:quality",
        "auto:economy",
        "auto:session",
    )
    default_aux_model = "auto:balanced"
    supports_vision = True
```

The provider should inject routing metadata through `build_extra_body()`:

```json
{
  "_hermes_auto": {
    "protocol_version": 1,
    "root_session_id": "<stable Hermes session identifier>",
    "virtual_model": "auto:balanced",
    "plugin_version": "0.1.0"
  }
}
```

Hermes provider profiles are declarative and are intentionally separate from client construction, credential rotation, and streaming. That makes the local gateway the correct place for the operational router.

Hermes already demonstrates the required session-metadata pattern in its OpenRouter profile: it obtains a stable conversation context and sends it in the provider body for sticky routing and cache locality.

The gateway must strip `_hermes_auto` before sending requests upstream.

## 5.2 General control plugin

A second module within the same distribution should register:

* CLI commands.
* Slash commands.
* Session lifecycle hooks.
* Post-turn and post-tool outcome reporters.
* Sidecar process supervision.
* Local health checks.
* Decision correlation.

It must **not** alter the chosen provider/model inside `AIAgent`. Its hooks are observational and administrative.

The distribution can still be installed and presented as one product:

```text
hermes-auto-router
├── Hermes provider component: hermes-auto
└── Hermes control component: hermes-auto-control
```

The provider activates when selected. The control component remains opt-in through `plugins.enabled`, matching Hermes’s third-party plugin model.

## 5.3 Local routing gateway

The gateway should provide:

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

The inference API must remain OpenAI Chat Completions compatible because Hermes’s normal chat transport already uses OpenAI-formatted messages and tools for many providers.

The administrative API should use a different authentication scope and preferably a separate local listener.

## 5.4 Internal gateway modules

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

---

# 6. Route identity and conversation semantics

A coding agent can make many model calls for one user request. Routing every call independently would create inconsistent tool behavior, destroy prompt caches, and make the agent’s trajectory unstable.

The router therefore needs four separate identities.

## 6.1 Root session

The Hermes conversation identifier.

Used for:

* User-visible session aggregation.
* Long-lived metrics.
* Explicit pinning.
* Session reset.
* Administrative controls.

## 6.2 Lane

A distinct agent or auxiliary execution stream within the root session.

Derive it from:

```text
lane_id = hash(
    root_session_id
    + normalized_system_prompt_hash
    + tool_schema_hash
    + initial_non_system_message_hash
)
```

This separates:

* Parent agent.
* Delegate subagents.
* Context compression.
* Vision analysis.
* Web extraction.
* Title generation.
* Other auxiliary calls.

This matters because Hermes may preserve the same root conversation context across delegate trees for cache affinity. The router should preserve that root relationship without forcing every child or auxiliary call onto the parent’s selected model.

## 6.3 User turn

Identify a user turn from the latest true `role=user` message preceding any assistant tool calls and `role=tool` results.

Every internal model call resulting from that user message must use the same route.

## 6.4 Cache epoch

A cache epoch ends when any of these occurs:

* A new session starts.
* The message history is no longer an extension of the previous history.
* Context compression substantially replaces or shortens prior history.
* The system/tool fingerprint changes.
* The user explicitly requests rerouting.
* The selected target becomes ineligible or unhealthy.
* A configured session TTL expires.

The router should compute a message fingerprint sequence and detect the longest common prefix between calls. A major prefix discontinuity creates a new cache epoch.

---

# 7. Routing pipeline

## 7.1 Step 1: Build a request feature record

The initial deterministic feature extractor should inspect:

### Text and structure

* Current user-message token count.
* Total context tokens.
* Turn count.
* Number of code fences.
* Presence of diffs.
* File paths and repository references.
* Stack traces and error messages.
* Test failures.
* Shell commands.
* Structured-output requirements.
* Planning or multi-step language.
* Ambiguous references such as “fix this” or “continue.”

### Agent state

* Number and types of available tools.
* Whether the request is an internal tool continuation.
* Number of prior tool calls in the turn.
* Recent tool errors.
* Current context utilization.
* Whether the lane is a parent, subagent, or inferred auxiliary lane.
* Whether images or other modalities are present.

### User and policy state

* Selected mode.
* Candidate allowlist and denylist.
* Local-only or data-residency policy.
* Maximum estimated turn cost.
* Maximum latency target.
* Explicit model pin.
* Whether experimental models are allowed.

Raw prompt text should be used transiently for classification but not persisted by default.

## 7.2 Step 2: Predict a capability requirement vector

Extend GitHub’s four dimensions into an agent-oriented vector:

```text
reasoning
code_generation
debugging
tool_orchestration
long_horizon_execution
context_synthesis
structured_precision
multimodal_reasoning
```

Each score is normalized to `[0, 1]`.

Domain tags should remain separate rather than being collapsed into capability scores:

```text
frontend_ui
backend_application
systems_infrastructure
database_sql
security
research
writing
mathematics
data_analysis
local_machine_operations
```

Additional policy classifications:

```text
task_risk: low | standard | high
latency_sensitivity: low | normal | high
privacy_requirement: cloud_allowed | approved_only | local_only
```

The first implementation should use deterministic rules. A learned requirement predictor should be added only after the baseline is measured.

## 7.3 Step 3: Apply hard eligibility filters

A candidate must be removed before scoring when any of these is true:

1. Disabled by the user or administrator.
2. Missing credentials or unreachable endpoint.
3. Disallowed provider or model.
4. Violates local-only, residency, or privacy policy.
5. Does not support required tools.
6. Does not support the request modality.
7. Cannot satisfy the input context plus output reserve.
8. Cannot produce the required output size.
9. Does not support required structured output or tool-call format.
10. Its harness adapter is unavailable or incompatible.
11. Its estimated cost exceeds a hard turn budget.
12. Its health circuit breaker is open.
13. Its configured API mode cannot safely replay the current transcript.

A learned component must never restore a hard-excluded candidate.

## 7.4 Step 4: Compute capability shortfall

For request requirement (r_k) and candidate capability (c_{m,k}):

[
S(m)=\sum_k w_k \max(0,r_k-c_{m,k})
]

Properties:

* Only deficiencies are penalized.
* Excess reasoning does not compensate for weak tool use.
* Excess code generation does not compensate for weak debugging.
* Dimension weights are mode- and workload-configurable.
* Debugging and tool orchestration should initially receive elevated weights for Hermes workloads.

A candidate is “sufficient” when:

[
S(m)\leq\tau_{\text{mode}}
]

`quality` uses the strictest threshold. `economy` allows a wider—but still bounded—shortfall.

## 7.5 Step 5: Calculate expected cost

The router should estimate the real turn cost, not only the model’s nominal input price:

[
C(m)=
T_{\text{uncached}}\cdot P_{\text{input}}
+
T_{\text{cached}}\cdot P_{\text{cache-read}}
+
T_{\text{cache-write}}\cdot P_{\text{cache-write}}
+
\widehat T_{\text{output}}\cdot P_{\text{output}}
+
F_{\text{request}}
]

Where:

* `T_uncached` is estimated uncached input.
* `T_cached` comes from the shared-prefix estimate.
* `T_cache-write` applies where providers charge cache writes.
* `T_output` is predicted from task type and historical candidate behavior.
* `F_request` represents provider-specific fixed fees.

The output-token estimate must be model-specific. Some models solve the same task with materially different reasoning and output-token usage.

Hermes already normalizes prompt, completion, cached, and reasoning-token usage, which can be reused to reconcile predictions against actual costs.

Unknown pricing should be handled explicitly:

* Allowed in `quality` only when the user permits unknown-cost models.
* Excluded from `economy` by default.
* Never represented as zero cost.

## 7.6 Step 6: Calculate latency and reliability risk

Maintain rolling statistics by the complete execution identity:

```text
(provider, model, endpoint, harness_version)
```

Track:

* Time to first token.
* Total generation time.
* Queue delay.
* 401, 402, 429, and 5xx rates.
* Empty-response rate.
* Invalid tool-call rate.
* Stream-interruption rate.
* Context-limit failures.
* Structured-output failures.
* Recent quality anomaly score.

For local deployments, include current queue depth and accelerator saturation where available. Recent serving research indicates that quality, cost, latency, and live instance load should not be treated as entirely independent scheduling concerns. ([arXiv][3])

Hard outages remain a veto. Soft latency degradation contributes a penalty.

## 7.7 Step 7: Add domain affinity and bounded outcome residual

Each model card may contain domain affinity:

```text
frontend_ui: 0.80
debugging: 0.72
shell_and_tools: 0.91
long_horizon: 0.65
```

A later learned residual estimates where measured Hermes outcomes differ from static capability expectations:

[
\Delta_{\text{learned}}(m,x)
]

It must be bounded:

[
-r_{\max}\leq\Delta_{\text{learned}}\leq r_{\max}
]

The residual may reorder candidates that are already within the safe capability band. It may not:

* Override hard filters.
* Select a candidate outside the mode’s maximum shortfall.
* Violate a hard cost cap.
* Override a user pin.
* Select an unhealthy model.

## 7.8 Step 8: Calculate final route loss

A practical minimization function is:

[
L(m)=
\alpha_{\text{mode}}S(m)
+
\beta_{\text{mode}}\widehat C(m)
+
\gamma_{\text{mode}}\widehat T(m)
+
\delta R_{\text{reliability}}(m)
+
\epsilon P_{\text{switch}}(m)
-----------------------------

## \zeta A_{\text{domain}}(m,x)

\operatorname{clamp}(\Delta_{\text{learned}})
]

Where:

* (S) is capability shortfall.
* (\widehat C) is normalized expected cost.
* (\widehat T) is expected latency.
* (R) is reliability risk.
* (P_{\text{switch}}) is the cache and transition penalty.
* (A) is domain affinity.
* (\Delta) is the bounded learned residual.

The router should remove Pareto-dominated candidates before final ranking. A model that is simultaneously more expensive, slower, less capable, and less reliable than another candidate does not need further consideration.

## 7.9 Step 9: Apply stickiness and hysteresis

Even when a different candidate has a slightly lower score, retain the current candidate unless:

[
L(\text{current})-L(\text{best})

>

H_{\text{mode}}+P_{\text{switch}}
]

The switch penalty should include:

* Lost prompt-cache value.
* Additional first-token latency.
* Model-specific harness transition risk.
* Potential provider-specific history incompatibility.
* A fixed coherence penalty.

This is the main Cursor-derived protection. Cursor reports that provider/model caches are specific and that switching forces a slower and more expensive first request while also exposing the new model to out-of-distribution history. ([Cursor][4])

## 7.10 Step 10: Apply health veto and select

The final semantic ranking should be preserved. Operational health then removes unavailable candidates without otherwise reshuffling the list.

```text
semantic ranking
    ↓
remove hard-unhealthy candidates
    ↓
first remaining candidate
```

This keeps explanations stable:

```text
Preferred model: coding-specialist
Health veto: active 429 cooldown
Selected model: frontier-general
```

---

# 8. Switching policy

## 8.1 Required behavior matrix

| Event                                     | Router behavior                                                       |
| ----------------------------------------- | --------------------------------------------------------------------- |
| First request in a lane                   | Perform full route selection                                          |
| Additional LLM call in the same tool loop | Reuse the locked turn route                                           |
| New user turn, same cache epoch           | Re-evaluate, but preserve current route unless hysteresis is exceeded |
| Context compression or major prefix reset | Start a new cache epoch and permit rerouting                          |
| Explicit `/auto reroute`                  | Start a new epoch and reroute                                         |
| Explicit pin                              | Bypass normal ranking while the candidate remains hard-eligible       |
| Candidate becomes hard-ineligible         | Switch at the next safe boundary                                      |
| Provider failure before streamed output   | Select next healthy ranked candidate                                  |
| Provider failure after output begins      | Do not splice models into the same stream; fail the request cleanly   |
| Subagent starts                           | Create a new lane and route it separately                             |
| Auxiliary task starts                     | Create a separate lane; use its task alias or infer its task          |
| Session reset                             | Discard session state                                                 |

## 8.2 First-chunk commit barrier

For streamed requests, the gateway should not commit the route to Hermes until:

1. The upstream connection succeeds.
2. The response status is valid.
3. The first valid SSE event is parsed.

Before that point, the gateway may transparently fail over.

After the first event is sent to Hermes, the route is committed. A second model must not continue the same output stream because that can produce:

* Duplicate text.
* Corrupt tool-call JSON.
* Conflicting tool identifiers.
* Inconsistent reasoning.
* Untraceable billing.

## 8.3 Auxiliary-task aliases

Recommended Hermes auxiliary configuration:

```yaml
auxiliary:
  compression:
    provider: hermes-auto
    model: auto:compression

  vision:
    provider: hermes-auto
    model: auto:vision

  title_generation:
    provider: hermes-auto
    model: auto:economy

  web_extract:
    provider: hermes-auto
    model: auto:economy
```

These aliases give the sidecar an explicit task signal even when the normal provider request does not include an auxiliary task name.

Hermes’s auxiliary router already prioritizes the active main provider and model before trying its broader fallback chain, so selecting `hermes-auto` can naturally bring many auxiliary calls through the router.

---

# 9. Model inventory and model cards

## 9.1 Candidate identity

A candidate is not merely a model ID. It is:

```text
provider
endpoint
model
credential scope
protocol adapter
harness profile
harness version
```

For example, the same base model through two providers may have different:

* Latency.
* Pricing.
* cache behavior.
* Tool-call reliability.
* Context limits.
* Rate limits.
* Reasoning controls.
* Safety filters.

They should therefore be separate candidates.

## 9.2 Model-card schema

```yaml
id: hosted-coding-specialist

enabled: true

transport:
  adapter: openrouter
  provider: openrouter
  model: <provider/model-id>
  credential_ref: env:OPENROUTER_API_KEY

capabilities:
  reasoning: 0.82
  code_generation: 0.94
  debugging: 0.90
  tool_orchestration: 0.88
  long_horizon_execution: 0.79
  context_synthesis: 0.84
  structured_precision: 0.87
  multimodal_reasoning: 0.00

affinities:
  frontend_ui: 0.68
  backend_application: 0.92
  systems_infrastructure: 0.86
  database_sql: 0.79

limits:
  context_tokens: 400000
  max_output_tokens: 64000
  tools: true
  vision: false
  structured_output: true

economics:
  input_per_million: null
  output_per_million: null
  cache_read_per_million: null
  cache_write_per_million: null

harness:
  profile: openai-coding-v1
  version: 1
  reasoning_policy: adaptive
  cache_strategy: provider_session_key

policy:
  privacy_class: cloud
  experimental: false
  allowed_modes:
    - quality
    - balanced

evidence:
  source_version: "2026-07-26"
  calibration_dataset: "hermes-routerbench-v1"
  sample_count: 0
```

## 9.3 Metadata sources

Merge metadata from four layers:

1. **Operator policy overrides**
2. **Measured Hermes performance**
3. **Curated capability and harness profiles**
4. **Registry defaults**

Hermes already maintains offline-first model information including tool support, vision, reasoning, context limits, output limits, and input/output/cache pricing. This should be reused where practical rather than duplicated.

Precedence should be:

```text
hard operator policy
    > live health
    > measured candidate statistics
    > curated model card
    > registry metadata
    > conservative unknown defaults
```

## 9.4 Virtual context limit

Because Hermes sees a virtual model, it needs a declared context window for its own preflight checks and compression decisions.

Configure:

```yaml
model:
  context_length: 256000
```

Hermes honors an explicit top-level model context length before endpoint probes and catalog fallbacks.

The router’s setup and doctor commands must validate that:

* At least one enabled **context-anchor candidate** supports the advertised virtual context.
* Every request is routed only to candidates that support its actual context.
* Smaller-context candidates remain eligible for smaller requests.
* The gateway returns an explicit context-capacity diagnostic when no candidate can satisfy a request.

Example:

```yaml
candidates:
  - id: frontier-context-anchor
    context_anchor: true
    limits:
      context_tokens: 400000
```

---

# 10. Provider and harness adapters

## 10.1 Adapter interface

```python
class BackendAdapter(Protocol):
    def validate_candidate(self, candidate) -> ValidationResult: ...
    async def health(self, candidate) -> HealthObservation: ...
    async def complete(self, request, candidate, harness): ...
    async def stream(self, request, candidate, harness): ...
    def normalize_response(self, response) -> CanonicalResponse: ...
    def normalize_usage(self, response) -> CanonicalUsage: ...
```

## 10.2 Adapter rollout order

### Tier 1: OpenAI-compatible aggregator and local endpoints

Initial support:

* OpenRouter or another OpenAI-compatible aggregator.
* Ollama.
* vLLM.
* LM Studio.
* llama.cpp-compatible servers.
* Standard custom OpenAI-compatible APIs.

Advantages:

* One request and response shape.
* Lower cross-provider replay risk.
* Easier streaming and tool-call validation.
* Rapid MVP delivery.
* Supports both hosted and private local candidates.

### Tier 2: Optional LiteLLM transport

LiteLLM can normalize requests across more than 100 providers, support OpenAI-shaped input/output, and provide fallback and spend-management functionality. It should be used as an **execution transport**, not as the Auto decision engine. ([LiteLLM][5])

The Hermes router must retain ownership of:

* Requirements.
* Model cards.
* Hard policy.
* Cost–quality modes.
* Session stickiness.
* Explanations.
* Outcome learning.
* Health policy.

Run LiteLLM in the isolated sidecar environment to prevent dependency conflicts with Hermes.

### Tier 3: Direct native adapters

Add direct adapters only after the gateway contract is stable:

* OpenAI Responses.
* Anthropic Messages.
* Gemini.
* Bedrock.
* Other native APIs.

Each adapter must normalize:

* System/developer roles.
* Tool definitions.
* Tool-call identifiers.
* Tool results.
* Images.
* Reasoning controls.
* Provider-specific signed or replay fields.
* Usage and cached-token reporting.

### Tier 4: Optional Hermes-native credential bridge

A version-gated adapter may import Hermes’s provider resolution and credential-pool code to access:

* ChatGPT/Codex OAuth.
* GitHub Copilot authentication.
* Claude Code OAuth.
* Nous credentials.
* Other Hermes-native provider flows.

Hermes has a centralized auxiliary client resolver that handles authentication, base URLs, and protocol adaptation while presenting a common completion interface.

This bridge should remain optional because it depends on internal Hermes modules. Requirements:

* Explicit supported-version range.
* Startup compatibility probe.
* Clear fail-closed behavior.
* Nightly CI against Hermes `main`.
* No undocumented mutation of Hermes state.
* A stable aggregator/local fallback when the bridge is unavailable.

## 10.3 Model-specific harness profile

A candidate should select a versioned harness profile containing:

```yaml
harness:
  system_overlay: null
  tool_schema_transform: openai-strict
  response_normalizer: openai-chat
  reasoning_adapter: openrouter-reasoning
  max_output_policy: dynamic
  temperature_policy: omit
  cache_strategy: session-key
  takeover_instruction: model-switch-v1
```

Version it as part of the candidate identity.

Initial scope should be conservative:

* Parameter adaptation.
* Schema sanitization.
* Reasoning configuration.
* Cache keys.
* Response normalization.
* Short takeover instructions after a model switch.

Do not initially replace the Hermes tool set or expose different tool names to different models. Fully model-specific tool sets introduce significant transcript-replay complexity and can undermine Hermes’s system-prompt stability.

---

# 11. Health, reliability, and fallback

## 11.1 Health model

Maintain separate health states for:

```text
provider
endpoint
model
credential
candidate/harness combination
```

Use passive observations plus inexpensive active checks.

Suggested health score inputs:

```text
successful request rate
429 frequency
5xx frequency
authentication failures
TTFT p50/p95
stream failure rate
empty-response rate
invalid tool-call rate
context-error rate
quality anomaly rate
queue depth
```

Use exponentially weighted moving averages with:

* Minimum sample thresholds.
* Short transient cooldowns.
* Longer circuit-breaker cooldowns.
* Half-open probes.
* Separate rate-limit and reliability states.

## 11.2 Fallback sequence

```text
1. Retry the same candidate only for explicitly retryable transport errors.
2. Rotate credentials within the same provider when supported.
3. Select the next healthy candidate from the original semantic ranking.
4. Recompute ranking only when the candidate inventory or hard policy changed.
5. Fall back to a configured emergency candidate.
6. Return a structured failure if no hard-eligible candidate remains.
```

Never silently violate:

* Hard budget.
* Local-only policy.
* Provider denylist.
* Context requirements.
* Tool requirements.

## 11.3 Sidecar outage

Provide three layers:

1. Control plugin automatically starts or reconnects to the local service.
2. Persistent service mode for long-running gateway/desktop use.
3. Optional Hermes `fallback_providers` entry pointing to a fixed safe provider.

The emergency fallback should be operator-approved because it causes Hermes itself to leave the virtual Auto provider for that session.

---

# 12. Explainability

Every route should produce a `RouteDecision` record:

```json
{
  "decision_id": "dec_...",
  "root_session_hash": "...",
  "lane_id": "...",
  "cache_epoch": 4,
  "turn_id": 12,
  "mode": "balanced",
  "requirements": {
    "reasoning": 0.78,
    "code_generation": 0.65,
    "debugging": 0.91,
    "tool_orchestration": 0.73
  },
  "excluded": [
    {
      "candidate": "local-fast",
      "reasons": [
        "context window 65536 < required 84211"
      ]
    }
  ],
  "ranked": [
    {
      "candidate": "coding-specialist",
      "shortfall": 0.03,
      "estimated_cost": 0.071,
      "estimated_ttft_ms": 620,
      "health": 0.97,
      "switch_penalty": 0.14,
      "final_loss": 0.21
    }
  ],
  "selected": "coding-specialist",
  "reason_codes": [
    "CAPABILITY_FIT",
    "DEBUGGING_AFFINITY",
    "COST_TIEBREAK"
  ]
}
```

Example user display:

```text
Selected: coding-specialist

Why:
- High debugging requirement: 0.91
- Tool orchestration requirement: 0.73
- Current local model was excluded because the request exceeded its context window
- The selected model met the capability floor at the lowest expected cost

Route retained for the remainder of this tool loop.
```

Reason codes:

```text
HARD_REQUIREMENT
CAPABILITY_FIT
DOMAIN_AFFINITY
COST_TIEBREAK
LATENCY_TIEBREAK
STICKY_CACHE
SWITCH_HYSTERESIS
USER_PIN
HEALTH_VETO
PROVIDER_FALLBACK
CONTEXT_ANCHOR
EMERGENCY_DEFAULT
```

Do not expose or attempt to generate hidden model reasoning. Explanations should be derived from router inputs, scores, and policy.

---

# 13. Telemetry and outcome learning

## 13.1 Local-first event store

Use SQLite in WAL mode for local installations:

```text
route_decisions
route_attempts
usage_events
health_observations
turn_outcomes
session_outcomes
model_cards
model_card_versions
router_versions
feedback
```

Default behavior:

* Store no raw prompt text.
* Store no tool-result bodies.
* Store no secrets.
* Hash root session identifiers with a local salt.
* Store derived features and numeric scores.
* Permit local deletion and retention controls.
* Make external export opt-in.

Hermes’s contribution policy explicitly requires outbound telemetry and attribution to be gated by an explicit user-facing opt-in.

## 13.2 Immediate operational outcomes

Collect automatically:

* API success or failure.
* Time to first token.
* Total latency.
* Actual input/output/cache/reasoning tokens.
* Actual cost.
* Tool-call count.
* Invalid tool-call count.
* Empty response.
* Retry count.
* Candidate fallback.
* Context compression.
* User interruption.

## 13.3 Turn-level outcomes

The control plugin can use Hermes lifecycle and tool hooks to report:

* Whether the turn completed successfully.
* Tool execution failures.
* Test command results.
* Whether the user immediately requested a correction.
* Whether the user retried or changed models.
* Explicit positive or negative feedback.
* Whether the session progressed to a different task.

## 13.4 Optional code outcomes

When explicitly enabled for a trusted repository:

* Generated diff retained after later turns.
* Edits reverted.
* Tests pass after changes.
* Commit created.
* Generated code survives to commit.
* Cost per successful commit.

These should be local metrics unless the user explicitly exports them.

## 13.5 External exporters

Optional adapters:

* Langfuse.
* OpenTelemetry.
* JSONL.
* Parquet.
* Prometheus metrics.
* Team-hosted analytics.

Export only:

* Derived routing features.
* Candidate IDs.
* Decisions.
* Usage.
* Outcomes explicitly approved by policy.

---

# 14. Learning strategy

## 14.1 Do not begin with a learned router

Recent large-scale router evaluation found that many sophisticated routers do not reliably outperform simple baselines under unified evaluation, and that careful model-pool curation is often more valuable than continually enlarging the candidate pool. ([arXiv][6])

Therefore, the order must be:

```text
hard filters
    → deterministic heuristic predictor
    → deterministic capability matcher
    → measured baseline
    → learned requirement predictor
    → bounded outcome residual
    → constrained online adaptation
```

## 14.2 Learned requirement predictor

Once sufficient data exists, train a compact encoder similar in role to HyDRA:

```text
Input:
  current user request
  bounded recent context
  deterministic metadata prefix

Output:
  eight capability requirements
  domain probabilities
  confidence and out-of-distribution score
```

Recommended deployment:

* ModernBERT-class encoder or smaller equivalent.
* ONNX.
* Dynamic INT8 CPU quantization where safe.
* Maximum bounded input.
* Strict classifier timeout.
* Deterministic heuristic fallback.
* Versioned calibration.

The model must predict capabilities, not specific target model IDs. This allows new models to be added by creating model cards rather than retraining the classifier.

## 14.3 Training labels

Create labels from incremental model value:

1. Run a cheaper and stronger candidate from the same router-visible state.
2. Evaluate both outputs.
3. Measure where the stronger model produced additional value.
4. Assign capability-specific delta labels.

For coding and tool tasks, prefer execution-derived evidence:

* Tests.
* Task completion.
* Correct tool sequence.
* Valid tool calls.
* Repository state.
* Official benchmark grading.

Use an LLM judge only for criteria that cannot be mechanically verified, such as:

* Clarity.
* UI quality.
* Subjective style.
* Helpfulness.
* Explanation quality.

Reduce judge bias through:

* Reversed answer order.
* Multiple judges or repeated judgments.
* Calibrated human subsets.
* Versioned judge prompts.

## 14.4 Learned outcome residual

The second learned layer should estimate:

```text
measured outcome
-
expected outcome from static capability matching
```

This captures effects such as:

* A model being unusually good at frontend work.
* Tool-call reliability differences.
* A particular harness improving one model.
* A provider version degrading.
* A local model performing better on a user’s recurring domain.

Start with an interpretable ranking or tree-based model over:

* Request features.
* Requirement vector.
* Candidate card.
* Domain tags.
* Harness version.
* Recent measured statistics.

Cold-start behavior for new models is residual `0`, preserving the deterministic baseline.

Preference-data routing and domain/action routing research support learning user-aligned differences while keeping model onboarding decoupled from the core classifier. ([arXiv][7])

## 14.5 Constrained contextual bandit

Only after the residual layer is stable should the router explore.

Constraints:

* Exploration is opt-in.
* Explore only among hard-eligible candidates.
* Explore only within a narrow capability-shortfall band.
* Never explore on high-risk tasks.
* Never violate hard budget or privacy policy.
* Log selection propensity.
* Maintain a fixed holdout group.
* Provide an immediate kill switch.
* Cap exploration percentage.
* Decay exploration when confidence rises.

---

# 15. Evaluation program

## 15.1 Required baselines

Compare against:

1. Cheapest candidate only.
2. Strongest candidate only.
3. User’s current fixed model.
4. Random eligible model.
5. Rule-based difficulty tiers.
6. Deterministic capability router.
7. Capability router plus domain affinity.
8. Capability router plus learned residual.
9. Oracle hindsight selector.

## 15.2 Metrics

### Quality

* Task success.
* Tests passed.
* Human or judge preference.
* Tool-call correctness.
* Structured-output validity.
* User correction rate.
* Repeated-attempt rate.
* Accepted-edit rate.
* Route regret against oracle.

### Economics

* Cost per request.
* Cost per completed turn.
* Cost per successful task.
* Cost per accepted edit.
* Cost per passing benchmark.
* Cost per commit where enabled.
* Cache savings lost to switches.

### Performance

* Router-decision latency.
* Time to first token.
* End-to-end latency.
* Tool-loop duration.
* Queue time.
* Model-switch rate.
* Cache hit and cached-token ratio.

### Reliability

* Request error rate.
* Invalid tool calls.
* Empty responses.
* Stream failures.
* Fallback success rate.
* Context-limit errors.
* Sidecar availability.

## 15.3 Agentic evaluation

One-shot prompt benchmarks are insufficient because an agent’s route affects later tool calls and downstream success. TwinRouterBench specifically addresses this problem by evaluating router-visible intermediate agent states and full trajectories rather than only initial prompts. ([arXiv][8])

Create a Hermes-specific evaluation harness with two tracks.

### Static replay track

Store sanitized router-visible snapshots:

```text
messages
tools
context statistics
session state
candidate inventory
health snapshot
model-card version
```

Run the router deterministically against them and calculate:

* Decision latency.
* Model selection.
* Estimated cost.
* Regret against known outcomes.
* Policy compliance.

### Dynamic execution track

Execute complete Hermes tasks in isolated repositories:

* Bug fixing.
* Feature implementation.
* Test repair.
* Refactoring.
* Multi-step shell tasks.
* Web research.
* Data extraction.
* Vision-assisted work.
* Context-compression scenarios.

Measure official task success and realized spend.

## 15.4 Proposed release gates

These are initial engineering targets to calibrate after baseline collection:

| Gate                                        |                                                          Initial target |
| ------------------------------------------- | ----------------------------------------------------------------------: |
| Deterministic router p99                    |                                                             Under 50 ms |
| Learned classifier p99 on CPU               |                                                            Under 100 ms |
| Added TTFT overhead                         |                                                  Under 1% or 100 ms p99 |
| Unexpected model changes within a tool loop |                                                                       0 |
| Hard-policy violations                      |                                                                       0 |
| Streaming/tool schema contract pass rate    |                                                100% in supported matrix |
| `balanced` quality regression               | No statistically significant regression against selected fixed baseline |
| `balanced` cost reduction                   |                                  At least 20% before default enablement |
| Pre-stream failover recovery                |                                  At least 99% in injected-failure tests |
| Raw prompt retention by default             |                                                                       0 |
| Deterministic replay with pinned snapshots  |                                                                    100% |

---

# 16. Configuration design

```yaml
plugins:
  enabled:
    - hermes-auto-control

model:
  provider: hermes-auto
  model: auto:balanced
  context_length: 256000
  base_url: http://127.0.0.1:8787/v1

auto_router:
  protocol_version: 1
  mode: balanced

  gateway:
    url: http://127.0.0.1:8787
    auto_start: true
    startup_timeout_seconds: 10
    state_dir: ~/.hermes/auto-router

  routing:
    stickiness: cache_boundary
    reroute_on_new_turn: true
    reroute_on_compression: true
    reroute_on_hard_failure: true
    switch_hysteresis: 0.15
    classifier_timeout_ms: 80
    safe_default_candidate: hosted-general
    show_selected_model: true

  constraints:
    local_only: false
    allow_experimental: false
    max_estimated_turn_cost_usd: 1.00
    max_ttft_ms: null
    allow_unknown_pricing: false
    allow_providers:
      - openrouter
      - local
    deny_models: []

  virtual_model:
    context_length: 256000
    max_output_tokens: 64000
    context_anchor_candidates:
      - hosted-general

  candidates:
    - id: local-fast
      enabled: true
      adapter: openai-compatible
      provider: local
      model: <local-model-id>
      base_url: http://127.0.0.1:11434/v1
      credential_ref: none
      roles:
        - cheap
        - private
        - fast

    - id: hosted-general
      enabled: true
      adapter: openrouter
      provider: openrouter
      model: <hosted-general-model>
      credential_ref: env:OPENROUTER_API_KEY
      context_anchor: true
      roles:
        - general
        - tools
        - long-context

    - id: hosted-coding
      enabled: true
      adapter: openrouter
      provider: openrouter
      model: <coding-model>
      credential_ref: env:OPENROUTER_API_KEY
      roles:
        - coding
        - debugging

    - id: frontier-reasoning
      enabled: true
      adapter: openrouter
      provider: openrouter
      model: <frontier-model>
      credential_ref: env:OPENROUTER_API_KEY
      allowed_modes:
        - quality
        - balanced

  telemetry:
    local_store: true
    retain_days: 30
    store_raw_prompts: false
    store_tool_results: false
    external_export: disabled

  learning:
    requirement_predictor: heuristic
    outcome_residual: disabled
    online_exploration: disabled
```

Only credentials and generated local bearer tokens belong in environment variables. Behavioral settings should remain in `config.yaml`, consistent with Hermes’s configuration rules.

---

# 17. Repository structure

```text
hermes-auto-router/
├── pyproject.toml
├── README.md
├── LICENSE
├── SECURITY.md
├── CHANGELOG.md
├── docs/
│   ├── architecture.md
│   ├── routing-policy.md
│   ├── privacy.md
│   ├── model-cards.md
│   ├── evaluation.md
│   ├── provider-adapters.md
│   └── troubleshooting.md
├── src/
│   └── hermes_auto/
│       ├── __init__.py
│       ├── version.py
│       ├── plugin.py
│       ├── provider.py
│       ├── commands.py
│       ├── cli.py
│       ├── config.py
│       ├── supervisor.py
│       ├── compatibility.py
│       ├── gateway/
│       │   ├── app.py
│       │   ├── auth.py
│       │   ├── schemas.py
│       │   ├── ingress.py
│       │   ├── streaming.py
│       │   ├── admin.py
│       │   └── errors.py
│       ├── routing/
│       │   ├── request.py
│       │   ├── features.py
│       │   ├── requirements.py
│       │   ├── heuristic_predictor.py
│       │   ├── learned_predictor.py
│       │   ├── domains.py
│       │   ├── eligibility.py
│       │   ├── shortfall.py
│       │   ├── cost.py
│       │   ├── latency.py
│       │   ├── scoring.py
│       │   ├── policy.py
│       │   ├── stickiness.py
│       │   ├── decision.py
│       │   └── explain.py
│       ├── state/
│       │   ├── sessions.py
│       │   ├── lanes.py
│       │   ├── cache_epochs.py
│       │   └── locks.py
│       ├── inventory/
│       │   ├── model_cards.py
│       │   ├── models_dev.py
│       │   ├── discovery.py
│       │   ├── validation.py
│       │   └── snapshots.py
│       ├── health/
│       │   ├── tracker.py
│       │   ├── circuit_breaker.py
│       │   ├── probes.py
│       │   └── anomalies.py
│       ├── adapters/
│       │   ├── base.py
│       │   ├── openai_compatible.py
│       │   ├── openrouter.py
│       │   ├── litellm.py
│       │   ├── openai_responses.py
│       │   ├── anthropic.py
│       │   └── hermes_native.py
│       ├── harnesses/
│       │   ├── base.py
│       │   ├── registry.py
│       │   ├── tool_schemas.py
│       │   ├── reasoning.py
│       │   ├── cache_keys.py
│       │   └── takeover.py
│       ├── telemetry/
│       │   ├── events.py
│       │   ├── sqlite.py
│       │   ├── outcomes.py
│       │   ├── retention.py
│       │   └── exporters.py
│       ├── evaluation/
│       │   ├── replay.py
│       │   ├── dynamic.py
│       │   ├── baselines.py
│       │   ├── metrics.py
│       │   └── reports.py
│       ├── learning/
│       │   ├── datasets.py
│       │   ├── labels.py
│       │   ├── train_requirements.py
│       │   ├── train_residual.py
│       │   ├── calibration.py
│       │   └── bandit.py
│       └── data/
│           ├── default-model-cards.yaml
│           ├── harness-profiles.yaml
│           └── schema/
├── tests/
│   ├── unit/
│   ├── property/
│   ├── contract/
│   ├── integration/
│   ├── e2e/
│   ├── fault/
│   ├── performance/
│   └── fixtures/
└── scripts/
    ├── benchmark.py
    ├── generate_model_card.py
    ├── validate_release.py
    └── run_compatibility_matrix.py
```

---

# 18. Development phases

## Phase 0 — Architecture, contracts, and baselines

### Objectives

Freeze the public contracts before implementing routing logic.

### Work

1. Create architecture decision records for:

   * Provider plugin plus local gateway.
   * Session/cache switching policy.
   * Model-card architecture.
   * Local telemetry and privacy.
   * Adapter isolation.
2. Define:

   * OpenAI request/response subset.
   * Streaming SSE contract.
   * Internal `_hermes_auto` metadata schema.
   * `RouteDecision` schema.
   * Model-card schema.
   * Outcome-event schema.
3. Pin supported Hermes versions and establish CI against:

   * Latest release.
   * Current `main`.
4. Assemble a representative fixed-model baseline corpus.
5. Establish current quality, cost, latency, cache, and tool-call baselines.
6. Produce a threat model.

### Exit criteria

* Schemas are versioned.
* Compatibility strategy is documented.
* Baseline tasks run reproducibly.
* No production routing has been introduced.

---

## Phase 1 — Provider plugin and single-target passthrough

### Objectives

Prove that Hermes can use the virtual provider without behavioral differences.

### Work

1. Register `hermes-auto` as a provider.
2. Register virtual model IDs.
3. Implement stable session metadata.
4. Implement:

   * `/v1/models`
   * `/v1/chat/completions`
   * Streaming
   * Tools
   * Usage
   * Errors
5. Forward all requests to one configured OpenAI-compatible target.
6. Build the control plugin and:

   * `hermes auto start`
   * `stop`
   * `status`
   * `doctor`
7. Add generated bearer-token authentication.
8. Support Windows, macOS, and Linux process supervision.
9. Preserve Hermes request and response semantics exactly.

### Exit criteria

* A fixed candidate behaves equivalently through the gateway and directly.
* Streaming text and tool calls work.
* CLI, TUI, gateway, desktop, and cron smoke tests pass.
* Sidecar restarts are safe.
* No raw prompt logging occurs.

---

## Phase 2 — Candidate inventory and model cards

### Objectives

Create a trustworthy, deterministic candidate registry.

### Work

1. Implement model-card loading and JSON Schema validation.
2. Import Hermes/model-registry metadata where available.
3. Add runtime endpoint discovery.
4. Add credential availability checks.
5. Add capability and domain override files.
6. Add context-anchor validation.
7. Add hard compatibility filters.
8. Implement `hermes auto models` and `doctor`.
9. Start with a curated pool of approximately three to six candidates:

   * Cheap/private local.
   * Fast hosted general.
   * Coding/debugging specialist.
   * Frontier reasoning/tool model.
   * Vision model where needed.

### Exit criteria

* Candidate inventory is deterministic.
* Every excluded candidate has a reason.
* Unknown prices and capabilities are treated conservatively.
* Model-card snapshot and version are included in each decision.

Careful curation is preferable to starting with dozens of candidates; larger pools can create diminishing returns and model-recall failures. ([arXiv][6])

---

## Phase 3 — Deterministic Auto MVP

### Objectives

Ship useful Auto behavior without machine learning.

### Work

1. Implement deterministic feature extraction.
2. Implement heuristic requirement prediction.
3. Implement eight-dimensional capability shortfall.
4. Implement mode-specific thresholds and weights.
5. Implement expected cost and output-token estimates.
6. Implement domain affinity.
7. Implement Pareto pruning.
8. Implement `quality`, `balanced`, and `economy`.
9. Implement route explanations.
10. Add shadow mode:

    * Router selects a hypothetical candidate.
    * Configured fixed candidate still serves the request.
11. Compare shadow decisions against fixed-model outcomes.

### Exit criteria

* Identical inputs and snapshots produce identical decisions.
* Hard policy cannot be overridden.
* Property tests pass.
* Shadow reports expose expected cost and candidate distribution.
* The router does not yet change models during active production sessions unless explicitly enabled.

---

## Phase 4 — Cache-aware session routing

### Objectives

Make routing safe for long-running agent trajectories.

### Work

1. Implement root-session, lane, turn, and cache-epoch state.
2. Derive lane IDs for parent, child, and auxiliary requests.
3. Lock routes across internal tool loops.
4. Detect:

   * New user turns.
   * Context compression.
   * Prefix discontinuities.
   * Session resets.
5. Implement cache-prefix estimates.
6. Implement switch-cost estimates.
7. Implement hysteresis.
8. Add:

   * Pin.
   * Unpin.
   * Reroute.
   * Session-only mode.
9. Add auxiliary virtual aliases.
10. Test concurrent Hermes sessions against one sidecar.

### Exit criteria

* No unexpected switch occurs inside a tool loop.
* Subagents route independently.
* Compression opens a new routing boundary.
* Cache-preserving decisions are visible in explanations.
* Concurrent sessions cannot overwrite one another’s routes.

---

## Phase 5 — Health, fallback, and resilient streaming

### Objectives

Make operational conditions part of safe selection.

### Work

1. Implement passive health observations.
2. Implement active probes.
3. Add credential-, provider-, model-, and candidate-level circuit breakers.
4. Add 401, 402, 429, timeout, and 5xx classification.
5. Add pre-stream fallback.
6. Add the first-chunk commit barrier.
7. Add per-candidate cooldowns.
8. Add local queue-depth signals.
9. Add emergency-default behavior.
10. Add fault-injection tests.

### Exit criteria

* Failures before first output can fall back safely.
* Failures after stream commitment never splice output from another model.
* Health veto preserves semantic ranking.
* Rate-limited credentials do not poison the whole provider.
* Hard constraints remain enforced during fallback.

---

## Phase 6 — Adapter and harness expansion

### Objectives

Expand beyond the lowest-risk OpenAI-compatible path.

### Work

1. Stabilize the `BackendAdapter` interface.
2. Add optional LiteLLM execution.
3. Add direct native adapters one at a time.
4. Add model-specific:

   * Reasoning controls.
   * Output caps.
   * Temperature policy.
   * Tool schema sanitization.
   * Cache keys.
   * Provider-field stripping.
5. Add versioned harness profiles.
6. Add takeover instructions for safe boundary switches.
7. Add optional Hermes-native credential bridge.
8. Build protocol-crossing transcript tests.

### Exit criteria

* Each adapter passes the same contract suite.
* Candidate identity includes harness version.
* Cross-provider histories do not leak incompatible fields.
* OAuth bridge failure degrades to supported adapters rather than corrupting sessions.

---

## Phase 7 — Telemetry, outcomes, and Hermes RouterBench

### Objectives

Create the evidence needed to justify and improve routing.

### Work

1. Implement local SQLite telemetry.
2. Add retention and deletion controls.
3. Add explicit feedback.
4. Connect Hermes post-turn and post-tool observations.
5. Implement static replay.
6. Implement dynamic task execution.
7. Run all fixed-model and deterministic-router baselines.
8. Produce cost–quality–latency frontier reports.
9. Add optional exporters.
10. Establish quality non-inferiority gates.

### Exit criteria

* A complete route is reproducible from pinned snapshots.
* Actual costs reconcile with provider usage.
* Router benefit is measured per successful task.
* External export remains disabled by default.
* Reports distinguish model quality from provider reliability.

---

## Phase 8 — Learned capability predictor

### Objectives

Replace brittle heuristics while preserving the deterministic matcher.

### Work

1. Build an opt-in training corpus.
2. Generate capability-specific incremental-value labels.
3. Create public and synthetic training data.
4. Train a compact multi-head encoder.
5. Calibrate output dimensions.
6. Add confidence and out-of-distribution detection.
7. Export to ONNX.
8. Add safe quantization.
9. Implement classifier timeout and heuristic fallback.
10. Run learned and heuristic predictors in shadow side by side.

### Exit criteria

* Learned routing regret is lower than heuristic routing on held-out data.
* Router latency stays within gate.
* Language and domain slices are evaluated separately.
* Low-confidence inputs fall back safely.
* Adding a new model does not require predictor retraining.

---

## Phase 9 — Outcome residual and constrained adaptation

### Objectives

Add the Cursor-like behavioral layer.

### Work

1. Train a bounded residual model.
2. Add candidate/harness outcome calibration.
3. Add user- and project-level local calibration only when sufficient evidence exists.
4. Add inverse-propensity-aware offline evaluation.
5. Run shadow A/B comparisons.
6. Introduce opt-in constrained exploration.
7. Add automatic rollback on:

   * Quality regression.
   * Cost regression.
   * Tool-call regression.
   * Latency regression.
8. Add drift detection for model/provider updates.

### Exit criteria

* Residual improves measured outcomes over deterministic routing.
* Hard-policy invariants remain intact.
* Exploration never leaves the safe candidate band.
* A kill switch immediately returns users to deterministic routing.
* Cold-start candidates default to residual zero.

---

## Phase 10 — Production hardening and release

### Objectives

Make the plugin maintainable outside a development checkout.

### Work

1. Isolate sidecar dependencies in a managed environment.
2. Add signed or checksummed release artifacts.
3. Add schema migrations.
4. Add configuration backup and rollback.
5. Add service installation for:

   * Windows.
   * launchd.
   * systemd.
6. Add startup repair and stale-PID handling.
7. Add load and soak tests.
8. Add dependency and secret scanning.
9. Publish:

   * Installation guide.
   * Privacy guide.
   * Model onboarding guide.
   * Troubleshooting guide.
   * Evaluation methodology.
10. Add nightly compatibility CI against Hermes `main`.

### Exit criteria

* Clean installation and uninstall.
* No Hermes core file modifications.
* Upgrade and rollback tests pass.
* The sidecar survives extended concurrent operation.
* Security and privacy checklist passes.
* Release metrics satisfy the selected rollout gates.

---

# 19. Recommended pull-request sequence

Keep changes reviewable and independently testable:

| PR | Scope                                                     |
| -: | --------------------------------------------------------- |
|  1 | Repository skeleton, schemas, ADRs, compatibility checks  |
|  2 | Provider profile and control plugin                       |
|  3 | Single-target OpenAI-compatible gateway                   |
|  4 | Streaming and tool-call contract suite                    |
|  5 | Model cards, inventory, and doctor                        |
|  6 | Hard eligibility filters and expected-cost engine         |
|  7 | Deterministic requirement predictor and shortfall scoring |
|  8 | Modes, Pareto selection, and explanations                 |
|  9 | Session, lane, turn, and cache-epoch state                |
| 10 | Stickiness, hysteresis, pinning, and reroute              |
| 11 | Health, circuit breakers, and pre-stream fallback         |
| 12 | Local telemetry and outcome correlation                   |
| 13 | Static replay and dynamic evaluation harness              |
| 14 | Additional provider/harness adapters                      |
| 15 | Learned requirement predictor                             |
| 16 | Bounded outcome residual                                  |
| 17 | Constrained exploration and A/B controls                  |
| 18 | Packaging, service management, and release hardening      |

Do not combine the learned router with the initial gateway PR. The deterministic system must become a reliable baseline before a model is trained to improve it.

---

# 20. Test plan

## 20.1 Unit tests

Cover:

* Feature extraction.
* Requirement rules.
* Capability shortfall.
* Model-card merging.
* Hard filters.
* Cost estimation.
* Output-token prediction.
* Pareto pruning.
* Mode weights.
* Switch penalties.
* Hysteresis.
* Health calculations.
* Reason codes.
* Config migrations.

## 20.2 Property tests

Required invariants:

1. An ineligible candidate is never selected.
2. A hard budget is never exceeded silently.
3. A capability surplus cannot cancel another capability deficit.
4. A pinned candidate is used only while hard-eligible.
5. Identical snapshots yield identical decisions.
6. Adding a strictly dominated candidate does not change the selected route.
7. A learned residual cannot cross a hard filter.
8. A health veto cannot reorder healthy semantic candidates.
9. No route changes inside a locked tool turn.
10. No secret appears in a route explanation or telemetry row.

## 20.3 Protocol contract tests

Test:

* Non-streaming text.
* Streaming text.
* Single and parallel tool calls.
* Fragmented tool-call arguments.
* Empty deltas.
* Usage in final stream chunks.
* Images.
* Structured outputs.
* Refusals.
* Context errors.
* Provider reasoning fields.
* Cancellation.
* Client disconnect.
* Malformed SSE.

## 20.4 Integration tests

Run against:

* Mock OpenAI-compatible server.
* Ollama or equivalent local endpoint.
* One aggregator.
* One direct native API.
* LiteLLM adapter.
* Optional Hermes-native credential bridge.

## 20.5 Hermes end-to-end tests

Exercise:

* CLI conversation.
* TUI.
* Messaging gateway.
* Desktop.
* Cron.
* Parent/subagent delegation.
* Context compression.
* Vision.
* Web extraction.
* Session reset.
* Multiple simultaneous sessions.

Hermes expects behavior-contract and real-path tests for changes involving resolution chains, configuration, I/O, and security boundaries; mocks alone are insufficient.

## 20.6 Fault injection

Inject:

```text
401 authentication failure
402 exhausted balance
429 rate limit
500/502/503/504
connection refusal
DNS failure
TLS failure
slow headers
slow first token
mid-stream disconnect
invalid JSON
invalid tool call
empty response
context overflow
router process crash
SQLite lock contention
corrupt model card
stale credential
```

## 20.7 Performance tests

Measure:

* Router throughput.
* Concurrent session count.
* Decision latency.
* State-lock contention.
* SQLite write latency.
* SSE forwarding overhead.
* Memory growth.
* Long-running gateway stability.
* Candidate health-update overhead.

---

# 21. Security and privacy threat model

| Threat                                  | Required mitigation                                                                       |
| --------------------------------------- | ----------------------------------------------------------------------------------------- |
| Prompt causes expensive escalation      | Hard cost ceiling, bounded requirement scores, escalation tier cap, anomaly detection     |
| Prompt attempts to modify router policy | Policy never derived from user text; no prompt-triggered configuration commands           |
| Local port accessed by another process  | Loopback binding, generated bearer token, no CORS, restrictive token-file permissions     |
| Secret leakage in logs                  | Central redaction, credential references rather than values, structured logging allowlist |
| Malicious model metadata                | Schema validation, source checksums, operator overrides, conservative defaults            |
| Sidecar dependency compromise           | Isolated environment, pinned dependencies, lockfile, vulnerability scanning               |
| Raw code exported unintentionally       | No raw storage by default, explicit export consent, local retention controls              |
| Cost estimate manipulation              | Reconcile predicted and actual usage, cap output, unknown price is never zero             |
| Router classifier attack                | Input truncation, score caps, OOD detection, deterministic fallback                       |
| Model response spoofs route metadata    | Gateway—not the target model—sets decision headers and logs                               |
| Mid-stream fallback corrupts tools      | First-chunk commit barrier; no post-commit model splicing                                 |
| Concurrent state collision              | Per-lane locks, transactional state updates, hashed compound keys                         |
| Experimental model degrades quality     | Shadow stage, minimum samples, anomaly circuit breaker, immediate disable                 |
| Hermes upgrade breaks private bridge    | Version gating, compatibility tests, bridge optional and fail-closed                      |

---

# 22. Optional fork-native track

A deeper implementation is possible in a Hermes fork, but it should remain a separate track because current upstream policy does not accept per-call plugin routing.

The fork-native architecture would add:

```python
class ModelRouteProvider(ABC):
    def select(self, context: RouteContext) -> RouteDecision | None:
        ...
```

`RouteContext` would include:

```text
call kind
main versus auxiliary
auxiliary task name
parent or subagent
session ID
turn ID
cache epoch
message/token statistics
tool list
active provider/model
available credentials
configured fallback chain
```

A public atomic runtime transition service would wrap the existing model-switching behavior rather than allowing plugins to mutate internal fields.

Potential benefits:

* Direct use of Hermes credential pools.
* Native OAuth providers.
* Exact auxiliary task labels.
* Exact turn and cache-boundary events.
* No local HTTP hop.
* Shared provider protocol conversion.
* Native UI model display.

Costs:

* Permanent fork divergence.
* More extensive cache and concurrency risk.
* Routing logic becomes coupled to every Hermes call site.
* Current upstream design-direction conflict.
* Greater regression surface across CLI, gateway, cron, auxiliary tasks, and delegates.

The recommended sequence is:

1. Implement and validate the standalone provider/gateway design.
2. Use it as the reference implementation and evaluation oracle.
3. Introduce a fork-native route SPI only when its measured benefits justify the maintenance burden.
4. Keep the same model cards, scoring engine, decisions, and test corpus so both integration modes remain behaviorally equivalent.

A smaller, potentially upstream-compatible future enhancement would merely pass richer call metadata—such as auxiliary task name or route lane—into `ProviderProfile.build_extra_body()`. That would improve external gateways without allowing plugins to replace Hermes’s active runtime.

---

# 23. Recommended first production release boundary

The first useful release should deliberately stop before machine learning.

## Include

* `hermes-auto` provider.
* Local supervised gateway.
* OpenAI-compatible streaming and tools.
* OpenRouter or equivalent aggregator adapter.
* Local OpenAI-compatible adapter for Ollama, vLLM, and LM Studio.
* Three to six curated candidates.
* Hard filters.
* Deterministic requirement scoring.
* Capability shortfall.
* Quality, Balanced, and Economy modes.
* Session, lane, turn, and cache-epoch state.
* Tool-loop route locking.
* Health veto and pre-stream fallback.
* Pinning and explicit reroute.
* Complete explanations.
* Local telemetry.
* Static shadow evaluation.
* No raw prompt retention.

## Exclude initially

* Online learning.
* Automatic exploration.
* Unbounded model catalogs.
* Mid-tool-loop switching.
* Post-stream model splicing.
* Fully model-specific tool sets.
* Direct dependence on private Hermes authentication internals.
* Automatic external telemetry.
* Remote multi-tenant service.
* Router decisions driven directly by one LLM judging another live request.

This first boundary already delivers most of GitHub’s strongest architectural properties and the practical parts of Cursor’s modes, domain awareness, cache accounting, and harness adaptation. The learned Cursor-like outcome layer should be added only after Hermes-specific trajectories demonstrate exactly where deterministic capability matching leaves measurable value on the table.

[1]: https://arxiv.org/abs/2605.17106 "https://arxiv.org/abs/2605.17106"
[2]: https://cursor.com/blog/router "https://cursor.com/blog/router"
[3]: https://arxiv.org/abs/2606.17949 "https://arxiv.org/abs/2606.17949"
[4]: https://cursor.com/blog/continually-improving-agent-harness "https://cursor.com/blog/continually-improving-agent-harness"
[5]: https://docs.litellm.ai/ "https://docs.litellm.ai/"
[6]: https://arxiv.org/abs/2601.07206 "https://arxiv.org/abs/2601.07206"
[7]: https://arxiv.org/abs/2406.18665 "https://arxiv.org/abs/2406.18665"
[8]: https://arxiv.org/abs/2605.18859 "https://arxiv.org/abs/2605.18859"
