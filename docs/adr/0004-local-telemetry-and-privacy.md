# ADR-0004: Local-first telemetry with no raw prompt retention

## Status

Accepted — 2026-07-26

## Context

The router cannot improve without evidence. Evaluating whether routing beats a fixed-model baseline,
detecting a regression, calibrating cost predictions against actual usage, and eventually training a
capability predictor all require a record of what was decided and what happened next.

But the data flowing through this router is the most sensitive data a developer has: source code,
credentials pasted into prompts, internal architecture, unreleased product plans, and tool results
containing file contents. A routing sidecar sits directly in that path. A telemetry design that
defaults to retaining request content would create an exfiltration surface that did not previously
exist, inside a process the user installed to *save money*.

Two constraints are external and non-negotiable:

- **Hermes's contribution policy explicitly requires outbound telemetry and attribution to be gated
  by an explicit user-facing opt-in** (`design.md §13.1`). A plugin that exports by default is not
  shippable in this ecosystem.
- A meaningful part of the target audience routes to **local models specifically for privacy**
  (Ollama, vLLM, LM Studio). For those users, any default network egress of derived data would
  defeat the reason they installed the product.

## Decision

**Storage.** Use **SQLite in WAL mode** as the local event store for local installations
(`design.md §13.1`). WAL mode is chosen so the gateway can write telemetry concurrently with readers
— the control plugin, `/auto stats`, and the admin API — without blocking the request path.

**Table set** (`design.md §13.1`):

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

**Default behaviors** (`design.md §13.1`):

- **Store no raw prompt text.**
- **Store no tool-result bodies.**
- **Store no secrets.**
- **Hash root session identifiers with a local salt.**
- Store only **derived features and numeric scores** — token counts, capability estimates, domain
  tags, costs, latencies, reason codes.
- **Permit local deletion and retention controls**, so a user can inspect, prune, or destroy their
  own history.
- **Keep external export opt-in.** No exporter — Langfuse, OpenTelemetry, JSONL, Parquet,
  Prometheus, team-hosted analytics — is enabled by default, and even when enabled an exporter may
  emit only derived routing features, candidate IDs, decisions, usage, and outcomes explicitly
  approved by policy (`design.md §13.5`).

The four prohibitions above (**no raw prompts, no tool-result bodies, no secrets, no default
external export**) are **hard defaults, not configuration recommendations**. They hold with zero
configuration, and PROJECT.md lists default raw-prompt retention among the project's zero-tolerance
gates.

**Opt-in is explicit.** Hermes's contribution policy requires outbound telemetry and attribution to
be gated behind an explicit user-facing opt-in, and this product honors that: enabling export is a
deliberate user action with a clear description of what leaves the machine (`design.md §13.1`).

Optional code-outcome collection — retained diffs, reverted edits, test results, commits — is
enabled only for a repository the user explicitly trusts, and those remain local metrics unless the
user separately exports them (`design.md §13.4`).

Downstream: **plan 01-04 writes the outcome-event schema against this record**, **plan 01-06 writes
the user-facing privacy documentation from it**, and Phase 8 implements the SQLite store.

## Consequences

### Positive

- **The privacy default is safe without configuration.** A user who installs the product and never
  opens the config file has already got the strongest posture; there is no "remember to turn off
  telemetry" step to forget.
- **Users can delete their own history.** Retention and deletion controls are first-class, so the
  store never becomes an unbounded, unauditable record of a developer's work.
- **The store is inspectable offline.** A single SQLite file with no network dependency can be
  opened with standard tooling, which makes the privacy claim verifiable rather than merely stated.
- Compliance with the Hermes contribution policy is structural rather than procedural, so the
  product stays distributable.

### Negative

- **Derived-feature-only storage limits post-hoc debugging of routing mistakes.** When a route was
  wrong, we have the feature vector and the score, not the prompt that produced them. Reproducing a
  bad decision requires the user to reconstruct the input, and some classes of bug will be
  diagnosable only from a user-supplied reproduction.
- **Salted hashing makes cross-machine correlation impossible by design.** The salt is local, so the
  same conversation on two machines hashes differently. Fleet-level analysis across installs is not
  merely disabled — it is unavailable, and no future feature can quietly re-enable it.
- **Training corpora for the learning phases require an explicit separate opt-in** rather than
  reusing the default store. The default store's contents are deliberately too thin to train a
  requirement predictor on, so the learning track carries its own consent and its own collection
  path.
- An opt-in export path means most installs contribute no data, so aggregate evaluation depends on
  a small, self-selected, and therefore biased population.

## Alternatives Considered

- **Store raw prompts for better debugging and training** — rejected. It would materially improve
  post-hoc analysis and give a ready-made training corpus, but it **violates Hermes contribution
  policy and creates an exfiltration surface** inside the most sensitive data path a developer has
  (`design.md §13.1`).
- **Default-on external export to a hosted analytics service** — rejected. It would give the fastest
  path to aggregate evaluation, but it **violates the explicit user-facing opt-in requirement** and
  breaks the privacy expectation of users who route to local models precisely to avoid egress
  (`design.md §13.1`, `design.md §13.5`).
- **No telemetry at all** — rejected. Maximally private and trivially compliant, but it **makes the
  evaluation program and the outcome-learning track impossible**: without route decisions, usage,
  and outcomes there is no way to show that routing beats a fixed-model baseline, and no signal to
  learn from (`design.md §13.2`, `design.md §13.3`).
