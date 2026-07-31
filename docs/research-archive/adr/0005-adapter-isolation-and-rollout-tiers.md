# ADR-0005: Four-tier adapter rollout with an isolated sidecar environment

## Status

Accepted — 2026-07-26

## Context

Behind the provider boundary (ADR-0001), the gateway must actually *call* the selected candidate.
Every provider family — OpenAI Responses, Anthropic Messages, Gemini, Bedrock, local OpenAI-shaped
servers — differs in system/developer roles, tool definitions, tool-call identifiers, tool results,
image handling, reasoning controls, provider-specific signed or replay fields, and usage and
cached-token reporting (`design.md §10.2`). Normalizing all of that is a large, open-ended surface.

Two forces pull in opposite directions. Breadth of provider support is a product requirement — the
target user has a mixed local and hosted inventory. But every adapter added before the gateway
contract is stable is an adapter that will need reworking, and each one multiplies the
**cross-provider replay risk**: the risk that a transcript recorded against one provider cannot be
faithfully replayed against another because of identifier or field differences.

A third force is dependency hygiene. Broad-coverage transport libraries carry large dependency
trees. The sidecar runs alongside a Hermes installation, and PROJECT.md requires sidecar
dependencies to be isolated so that nothing the router needs can conflict with what Hermes needs.

## Decision

Adapters roll out in **four ordered tiers**, and the sidecar runs in its **own isolated Python
environment** so that no adapter's dependency tree can conflict with Hermes's (`design.md §10.2`).

**Tier 1 — OpenAI-compatible aggregator and local endpoints.** OpenRouter or another
OpenAI-compatible aggregator, plus Ollama, vLLM, LM Studio, llama.cpp-compatible servers, and
standard custom OpenAI-compatible APIs. This tier is first because it gives **one request and
response shape**, the **lowest cross-provider replay risk**, easier streaming and tool-call
validation, the **fastest path to a working MVP**, and because it **covers both hosted and private
local candidates** — the full breadth of the target inventory — with a single adapter
(`design.md §10.2`).

**Tier 2 — Optional LiteLLM, as an execution transport only.** LiteLLM normalizes requests across
more than 100 providers with OpenAI-shaped input and output. It is used as an **execution
transport**, and **never as the Auto decision engine** (`design.md §10.2`). It runs **inside the
isolated sidecar environment to prevent dependency conflicts with Hermes** (`design.md §10.2`).

Regardless of which transport executes a request, **the router retains ownership of**
(`design.md §10.2`):

```text
requirements
model cards
hard policy
cost-quality modes
session stickiness
explanations
outcome learning
health policy
```

**Tier 3 — Direct native adapters**, added **one at a time and only after the gateway contract is
stable**: OpenAI Responses, Anthropic Messages, Gemini, Bedrock, and other native APIs. Each must
normalize system/developer roles, tool definitions, tool-call identifiers, tool results, images,
reasoning controls, provider-specific signed or replay fields, and usage and cached-token reporting
(`design.md §10.2`). Sequencing them after contract stability is what keeps replay risk bounded.

**Tier 4 — Optional, version-gated Hermes-native credential bridge.** A version-gated adapter may
import Hermes's provider resolution and credential-pool code to reach ChatGPT/Codex OAuth, GitHub
Copilot authentication, Claude Code OAuth, Nous credentials, and other Hermes-native provider flows.
Because it depends on internal Hermes modules it **stays optional** and requires an explicit
supported-version range, a startup compatibility probe, **clear fail-closed behavior**, nightly CI
against Hermes `main`, no undocumented mutation of Hermes state, and **a stable aggregator/local
fallback when the bridge is unavailable** — that is, it must **fail closed and degrade to the
supported adapters**, never fail the product (`design.md §10.2`).

Harness profiles are versioned and are part of the candidate identity (see ADR-0003), so the
adapter, harness profile, and harness version travel together (`design.md §10.3`).

## Consequences

### Positive

- **A working MVP is reachable with one adapter.** Tier 1 alone covers hosted aggregators and every
  common local server, so the product is useful before any native adapter exists.
- **Dependency conflicts with Hermes are structurally prevented**, not merely avoided by care: the
  sidecar's environment is separate, so even a large transport dependency tree cannot reach the
  Hermes installation.
- **The credential bridge is optional, so a Hermes upgrade cannot break the product.** The fail-closed
  requirement plus the mandatory fallback means the worst case of an incompatible Hermes version is
  losing access to native OAuth providers, not losing the router.
- Policy ownership stays in one place regardless of transport, so swapping transports cannot
  silently change routing behavior.
- Deferring native adapters until the contract is stable keeps the replay corpus valid instead of
  invalidating it with every adapter added.

### Negative

- **Tier 1 alone cannot reach models without an OpenAI-compatible surface.** Until Tier 2 or Tier 3
  lands, some frontier models are reachable only through an aggregator that fronts them, inheriting
  that aggregator's pricing, latency, and availability.
- **LiteLLM adds a large dependency tree even when isolated.** Isolation prevents conflicts with
  Hermes; it does not prevent install size, cold-start cost, or the maintenance burden of tracking a
  fast-moving dependency's behavior changes.
- **The Tier 4 bridge requires nightly CI against Hermes `main` to stay viable.** It couples us to
  internal modules that carry no compatibility guarantee, so keeping it working is a standing
  operational cost with no upstream contract behind it.
- Four tiers means the adapter surface is deliberately incomplete for several phases, and users with
  a native-only provider will have to wait or route through an aggregator.

## Alternatives Considered

- **Build native adapters first** — rejected. It would maximize provider fidelity early, but it
  carries the **highest cross-provider replay risk before the gateway contract is stable**, and every
  adapter written against an unstable contract has to be reworked (`design.md §10.2`).
- **Make LiteLLM the routing engine** — rejected. LiteLLM offers fallback and spend-management
  features that overlap with ours, but adopting them **surrenders policy ownership**, which
  `design.md §10.2` explicitly forbids: the router must retain requirements, model cards, hard
  policy, cost-quality modes, session stickiness, explanations, outcome learning, and health policy.
- **Require the Hermes credential bridge** — rejected. Mandating it would unlock native OAuth
  providers immediately, but it **couples the product to private Hermes internals**, contradicting
  ADR-0001's whole premise that we depend only on supported extension points (`design.md §10.2`).
