# ADR-0001: Route behind the Hermes provider boundary, not inside `AIAgent`

## Status

Accepted — 2026-07-26

## Context

Hermes Agent deliberately treats **prompt caching as a core invariant** and prefers capabilities to
live at plugin and provider edges rather than expanding the central agent loop. Its development
guidance explicitly warns against cache-breaking model changes and against plugins that modify core
files (`design.md §1`).

Hermes does support a substantial plugin surface: pip-distributed plugins, slash commands and CLI
subcommands, model-provider plugins, provider-specific request metadata, session identifiers in
provider requests, and a shared inference path across every Hermes surface (`design.md §1`).

However, **current Hermes policy rejects plugin-controlled, per-call model/provider overrides inside
`AIAgent`**. A proposed failover-routing hook was closed unmerged, with the maintainers explicitly
directing users toward configured providers and fallback chains rather than plugin-selected live
routing (`design.md §1`).

This is the controlling constraint. Any design that requires reaching into `AIAgent` to swap the
active model per call is in direct conflict with upstream direction, has no supported API to build
on, and would depend on private fields whose behavior can change without notice.

The question this record answers is therefore not *how* to route, but *where the router is allowed
to live*.

## Decision

**The supported provider boundary is the integration point. The router lives behind that boundary,
not inside `AIAgent`** (`design.md §1`).

Concretely:

- The distribution registers a Hermes model-provider plugin named `hermes-auto` whose
  `ProviderProfile` is thin and declarative, advertising **virtual models** (`auto:balanced`,
  `auto:quality`, `auto:economy`, `auto:session`) and a loopback `base_url` (`design.md §5.1`).
- Hermes continues to believe it is talking to **one stable provider and one virtual model**. It
  configures `provider: hermes-auto`, `model: auto:balanced`, and a declared `context_length`
  (`design.md §1`).
- The provider injects routing metadata through the supported `build_extra_body()` hook under a
  `_hermes_auto` key — protocol version, root session identifier, virtual model, plugin version.
  The gateway strips this key before sending anything upstream (`design.md §5.1`).
- A **local OpenAI-compatible routing gateway**, running as a supervised sidecar process, receives
  the request, performs the actual candidate selection, invokes the real provider and model, and
  normalizes the response back into the shape Hermes expects (`design.md §1`, `design.md §5.3`).
- A second, opt-in control plugin (`hermes-auto-control`) provides setup, status, explanations,
  pinning, feedback, and sidecar lifecycle management. Its hooks are **observational and
  administrative only**; it must not alter the chosen provider or model inside `AIAgent`
  (`design.md §5.2`).

The gateway's inference API stays OpenAI Chat Completions compatible because Hermes's normal chat
transport already uses OpenAI-formatted messages and tools for many providers. The administrative
API uses a different authentication scope and preferably a separate local listener
(`design.md §5.3`).

This keeps the product a genuine Hermes plugin while avoiding private runtime mutation, upstream
policy conflict, and fork maintenance (`design.md §1`).

## Consequences

### Positive

- **No Hermes core modification.** Nothing in this repository reaches into a Hermes checkout.
- **No private runtime mutation.** The plugin uses only documented extension points, so a Hermes
  upgrade cannot break us by changing an internal field.
- **No fork maintenance.** We track upstream rather than diverging from it.
- **Works across every Hermes surface** — CLI, TUI, gateway, desktop, cron, and subagents — because
  they share one inference path and all of them resolve the same provider profile
  (`design.md §1`).
- **Routing dependencies stay isolated** from the Hermes environment. The sidecar owns its own
  dependency tree, so nothing the router needs can conflict with what Hermes needs
  (`design.md §3`).
- The stable OpenAI interface makes the gateway independently testable, replayable, and usable
  outside Hermes.

### Negative

- **Requires a supervised sidecar process across three operating systems** — Windows, macOS, and
  Linux — with service installation, health checks, restart policy, and an outage story
  (`design.md §3`).
- **Adds a local HTTP hop to every request**, which costs latency and adds a failure mode that
  would not exist in-process.
- **Loses access to exact auxiliary task labels and native credential pools.** Hermes knows whether
  a call is compression, vision, title generation, or web extraction; over the provider boundary we
  must infer it or rely on configured auxiliary aliases. Hermes-native OAuth credential pools are
  likewise not directly reachable (`design.md §22`).
- **The gateway must reconstruct turn and cache-boundary information that Hermes knows natively.**
  Turn boundaries, cache epochs, and lane identity have to be derived from message history rather
  than being handed to us as events — see ADR-0002 for the mechanism and its fragility
  (`design.md §22`).

## Alternatives Considered

- **Mutate `AIAgent` from a normal plugin** — rejected. Lowest routing latency and direct access to
  Hermes clients, but there is **no supported route-replacement API**, it requires private-field
  coupling, it conflicts with current upstream policy, and it is likely to introduce cache bugs
  (`design.md §3`).
- **Add a core route-selector hook (fork-native `ModelRouteProvider` SPI)** — rejected for this
  project. It would give native credentials and exact task metadata, but **current maintainers have
  rejected this direction** and it requires permanent fork maintenance, couples routing logic to
  every Hermes call site, and enlarges the regression surface across CLI, gateway, cron, auxiliary
  tasks, and delegates. Retained as an **optional fork-only track**, to be revisited only when
  measured benefits justify the maintenance burden; the standalone gateway becomes the reference
  implementation and evaluation oracle first (`design.md §3`, `design.md §22`).
- **Use delegation alone** — rejected as a complete solution. Delegation is supported by Hermes and
  its fresh context is cache-friendly, but it **cannot select the parent conversation model** and
  therefore cannot transparently implement Auto. It remains complementary, not a substitute
  (`design.md §3`).
- **Use a remote routing gateway** — rejected for now. Centralized team policy and telemetry are
  real benefits, but the **privacy, latency, deployment, and service-operability burden** is
  disproportionate for a single-developer install. Deferred to a later enterprise mode
  (`design.md §3`).
