# ADR-0003: Candidates are identity tuples described by versioned model cards

## Status

Accepted — 2026-07-26

## Context

The router must compare things it can actually route to. The naive unit of comparison is a model ID
— `claude-sonnet-4`, `qwen3-coder`, `gpt-5-mini` — but a model ID is not a routable thing. It is a
name that says nothing about *how* the request will be executed or *what it will cost*.

The same base model reached through two different providers can differ materially in latency,
pricing, cache behavior, tool-call reliability, context limits, rate limits, reasoning controls, and
safety filters (`design.md §9.1`). Routing that treats them as one candidate cannot express "prefer
this model, but only through that provider" — which is precisely the decision an operator with a
mixed local and hosted inventory needs to make.

Separately, the router needs *metadata* about each candidate: capability scores, domain affinities,
context and output limits, pricing, harness profile, and privacy class. That metadata arrives from
several sources of very different trustworthiness — operator configuration, live health probes,
measured local statistics, hand-curated cards, and provider registries — and they will disagree.
Hermes already maintains offline-first model information (tool support, vision, reasoning, context
limits, output limits, and input/output/cache pricing) that should be reused rather than duplicated
(`design.md §9.3`). Without an explicit precedence order, "which value wins" becomes an accident of
load order.

## Decision

**1. A candidate is an identity tuple, not a model ID.**

A candidate is the tuple (`design.md §9.1`):

```text
provider
endpoint
model
credential scope
protocol adapter
harness profile
harness version
```

The same base model reached through two providers is **two separate candidates**, because it may
differ in latency, pricing, cache behavior, tool-call reliability, context limits, rate limits,
reasoning controls, and safety filters (`design.md §9.1`). Rolling statistics, health state, and
pricing are all keyed by this full execution identity, and the harness version is part of the
identity so that changing the harness invalidates measurements taken under the old one
(`design.md §10.3`).

**2. Each candidate is described by a versioned model card.**

A model card is a declarative document carrying `transport`, `capabilities`, `affinities`, `limits`,
`economics`, `harness`, `policy`, and `evidence` blocks (`design.md §9.2`). The `evidence` block
records the card's source version, calibration dataset, and sample count, so any decision can record
**which card version produced it**.

**3. Metadata resolves through a six-level precedence chain, in exactly this order**
(`design.md §9.3`):

```text
hard operator policy
    > live health
    > measured candidate statistics
    > curated model card
    > registry metadata
    > conservative unknown defaults
```

Read strictly: hard operator policy overrides everything below it; live health overrides measured
statistics and everything below; measured candidate statistics override the curated card; the
curated card overrides registry metadata; and conservative unknown defaults apply only where no
higher level supplies a value. Registry metadata means Hermes's existing offline-first model
information, reused rather than duplicated (`design.md §9.3`).

**Unknown pricing is never represented as zero.** A missing price is an explicit unknown, handled by
policy — permitted in `quality` mode only when the user allows unknown-cost models, excluded from
`economy` by default — and never silently treated as free (`design.md §7.5`). Zero-cost defaults
would make the cheapest candidate always be the one we know least about, which inverts the intended
behavior.

Downstream: **plan 01-04 writes the model-card JSON Schema against this record**, and the loading,
validation, and merge implementation is Phase 3 work. This record freezes the identity tuple and the
precedence order, not the scoring that consumes them.

## Consequences

### Positive

- **New models are onboarded by writing a card, not by changing code.** Adding a candidate is a data
  change, reviewable as data and shippable without a release.
- **The same model through two providers can be scored differently**, so an operator can express
  provider preferences, privacy classes, and cost differences that a model-ID-keyed design cannot
  represent at all.
- **Every decision can embed the card version it used**, via the `evidence` block, which makes
  routing decisions reproducible in replay and makes it possible to attribute a regression to a
  specific card revision.
- The explicit precedence chain means "which source won" is answerable for any field, which is a
  prerequisite for user-facing explanations.
- Operator policy sitting at the top of the chain guarantees that no lower layer — including any
  learned component — can restore a candidate that policy excluded.

### Negative

- **Card curation is ongoing manual work.** Capability and affinity scores are judgments that need
  periodic recalibration against measurement; they do not maintain themselves.
- **Cards can drift from provider reality.** A provider changes pricing, a context window, or a
  tool-calling implementation, and the card keeps asserting the old value until someone notices.
  Live health and measured statistics mitigate this only for the fields they cover.
- **A corrupt or malicious card is a supply-chain surface.** A card that inflates capability scores
  or understates cost can steer every routing decision. This requires JSON Schema validation on
  load, checksums or signatures for cards obtained from outside the repository, and treating card
  ingestion as untrusted input.
- Six precedence levels are six places a value can come from, which makes debugging a surprising
  merge result harder than reading one file.

## Alternatives Considered

- **Treat a model ID as the candidate** — rejected. Simple and familiar, but it **loses
  provider-specific behavior differences** in latency, pricing, cache behavior, tool-call
  reliability, limits, and safety filtering, and it makes provider-scoped policy inexpressible
  (`design.md §9.1`).
- **Derive all metadata live from provider APIs** — rejected. Always-current in principle, but
  **unreliable, rate-limited, and unavailable offline**, which is disqualifying for a router that
  must work against local endpoints with no internet connection and must make a decision on the
  request's critical path (`design.md §9.3`).
- **Use only Hermes registry metadata** — rejected as the sole source. It is genuinely useful and is
  reused as the registry layer of the precedence chain, but it **lacks capability scores, domain
  affinities, and harness profiles**, which are exactly the fields routing decisions turn on
  (`design.md §9.3`).
