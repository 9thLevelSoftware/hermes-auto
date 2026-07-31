# Architecture Decision Records

## What an ADR is in this project

An **architecture decision record** captures a single decision that is expensive to reverse — an
integration boundary, a data-identity model, a privacy posture, a dependency-isolation strategy.
Each record is written once and, when it stops being true, **superseded by a new record** rather
than edited in place. The history of what we believed and why is part of the artifact.

`design.md` remains the **design rationale document**: the long-form, 2400-line analysis that
explains the whole system and the research it draws on. It is read-only for implementation plans
and it will keep growing. ADRs are the **citable decision records extracted from it** — short,
individually numbered, individually revisable, and referenced by number from plans, code comments,
and later phases. When an ADR and `design.md` disagree, that is a defect to be resolved explicitly
by superseding the ADR, not by silently editing either document.

Every ADR cites its source sections inline in the form `design.md §5.3`. A decision with no
`design.md` citation is not traceable and does not belong here.

## Numbering convention

- Four-digit, zero-padded number: `0001`, `0002`, … `0042`.
- Filename is `NNNN-kebab-case-title.md`.
- Numbers are assigned on creation and **never renumbered once merged**, even if a record is later
  superseded or withdrawn. Gaps are acceptable; renumbering breaks every inbound citation.
- A superseded record stays in place with its `## Status` updated to `Superseded by ADR-NNNN`.

## Template

```markdown
# ADR-NNNN: <Imperative statement of the decision>

## Status

Accepted — YYYY-MM-DD

<!-- One of: `Accepted`, `Proposed`, `Superseded by ADR-NNNN`, followed by the date. -->

## Context

The forces in play: the constraint, the upstream policy, the failure mode being avoided.
State facts, not preferences. Cite the source sections as `design.md §N`.

## Decision

What we are doing, in the active voice and the present tense. This section is the contract that
later plans implement against, so it must be specific enough to verify. Cite `design.md §N`.

## Consequences

### Positive

- What this buys us.

### Negative

- What this costs us, what it forecloses, and what new work it creates.

<!-- A record with no stated negative consequence is incomplete and WILL FAIL REVIEW.
     Every real decision has a cost. If none is written down, the decision was not analyzed. -->

## Alternatives Considered

- **<Option>** — one-line reason it was rejected, drawn from the decision column of the
  `design.md` §3 comparison table or from the section that evaluated it.
```

## Index

| ADR | Title | Status | Supersedes |
|-----|-------|--------|------------|
| [0001](0001-provider-plugin-and-local-gateway.md) | Route behind the Hermes provider boundary, not inside `AIAgent` | Accepted | — |
| [0002](0002-session-and-cache-switching-policy.md) | Route on cache boundaries using a four-part route identity | Accepted | — |
| [0003](0003-model-card-architecture.md) | Candidates are identity tuples described by versioned model cards | Accepted | — |
| [0004](0004-local-telemetry-and-privacy.md) | Local-first telemetry with no raw prompt retention | Accepted | — |
| [0005](0005-adapter-isolation-and-rollout-tiers.md) | Four-tier adapter rollout with an isolated sidecar environment | Accepted | — |

See [`../architecture.md`](../architecture.md) for the system overview these records decompose.

## What does *not* belong in an ADR

Routing algorithm internals — scoring formulas, dimension weight values, hysteresis thresholds,
circuit-breaker constants — are **implementation choices**, not architecture. They change with
measurement and are owned by the routing phases. ADRs freeze the shape of the system, not its
tuning.
