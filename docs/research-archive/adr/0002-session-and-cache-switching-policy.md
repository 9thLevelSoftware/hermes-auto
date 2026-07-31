# ADR-0002: Route on cache boundaries using a four-part route identity

## Status

Accepted — 2026-07-26

## Context

A coding agent makes **many model calls for one user request**. A single user instruction expands
into a system prompt, a plan, several tool calls, tool results fed back for interpretation,
follow-up reasoning, and often auxiliary calls for compression, vision, or title generation.

Routing every one of those calls independently would:

- **destroy prompt caches**, because each switch abandons the shared prefix the previous target had
  already cached;
- produce **inconsistent tool behavior**, because different models handle tool schemas, identifiers,
  and result formats differently; and
- **destabilize the agent's trajectory**, because mid-task the reasoning style, verbosity, and
  failure modes change underneath the agent (`design.md §6`).

Prompt-cache preservation is a hard Hermes invariant, and cache misses plus harness transitions
dominate the real cost of switching mid-conversation. The router therefore cannot treat "a request"
as the unit of routing. It needs a richer notion of identity — and because it sits behind the
provider boundary (ADR-0001), it must **derive** that identity from the message stream rather than
being told (`design.md §6`, `design.md §22`).

## Decision

The router maintains **four separate identities** for every request (`design.md §6`):

1. **Root session** — the Hermes conversation identifier, delivered in the `_hermes_auto` envelope.
   It is the unit of *user-visible aggregation*: session-level metrics, explicit pinning, session
   reset, and administrative controls (`design.md §6.1`).

2. **Lane** — a distinct agent or auxiliary execution stream *within* a root session. It is derived
   by hashing the root session identifier together with the normalized system-prompt hash, the
   tool-schema hash, and the initial non-system message hash (`design.md §6.2`). This separates the
   parent agent from delegate subagents, context compression, vision analysis, web extraction,
   title generation, and other auxiliary calls. Hermes may preserve the same root conversation
   context across delegate trees for cache affinity; lane identity **preserves that root
   relationship without forcing every child or auxiliary call onto the parent's selected model**
   (`design.md §6.2`).

3. **User turn** — identified from the **latest true `role=user` message preceding any assistant
   tool calls and `role=tool` results**. Every internal model call resulting from that user message
   uses the same route (`design.md §6.3`).

4. **Cache epoch** — the span over which a cached prefix stays valid. An epoch **ends** when any of
   the following occurs (`design.md §6.4`):
   - a new session starts;
   - the message history is no longer an extension of the previous history (prefix discontinuity);
   - context compression substantially replaces or shortens prior history;
   - the system/tool fingerprint changes;
   - the user explicitly requests rerouting;
   - the selected target becomes ineligible or unhealthy;
   - a configured session TTL expires.

   The router computes a message fingerprint sequence and detects the longest common prefix between
   calls; a major prefix discontinuity opens a new cache epoch (`design.md §6.4`).

Two **hard switching rules** follow from these identities and are not negotiable
(`design.md §8.1`, `design.md §8.2`):

- **Routes lock across internal tool loops.** The first request in a lane performs full route
  selection; every additional model call inside the same tool loop reuses the **locked turn route**.
  A new user turn within the same cache epoch re-evaluates but preserves the current route unless
  hysteresis is exceeded. A candidate that becomes hard-ineligible is switched away from **at the
  next safe boundary**, not immediately mid-loop (`design.md §8.1`).

- **The first-chunk commit barrier.** For streamed requests the gateway does not commit the route to
  Hermes until the upstream connection succeeds, the response status is valid, and the first valid
  SSE event is parsed. Before that point the gateway may transparently fail over to the next healthy
  ranked candidate. **After the first valid SSE event has been forwarded to Hermes, the route is
  committed and a second model must never be spliced into the same output stream.** A provider
  failure after that point fails the request cleanly (`design.md §8.2`).

This record deliberately does **not** specify hysteresis thresholds, switch-cost weights, or
re-evaluation scoring. Those are routing-implementation tuning owned by a later phase; only the
identity model and the two hard barriers are frozen here.

## Consequences

### Positive

- **Prompt-cache value is preserved.** Routing decisions land on natural cache boundaries instead of
  invalidating a warm prefix on every agent step.
- **Subagents route independently while preserving the root relationship.** A delegate can use a
  cheap model without dragging the parent conversation with it, and both still roll up to one
  user-visible session.
- **Tool loops stay coherent.** Every call arising from one user instruction is answered by the same
  model with the same harness, so tool-call identifiers, schemas, and reasoning style stay
  consistent for the whole loop.
- **A mid-stream provider failure cannot corrupt tool-call JSON**, because no second model is ever
  allowed to continue a committed stream.
- Route identity is a stable key for telemetry, explanations, and replay: a decision can be
  attributed to a specific (session, lane, turn, epoch).

### Negative

- **The router must reconstruct turn boundaries by inspecting message history rather than being told
  them.** Hermes knows exactly where a turn starts; behind the provider boundary we infer it. That
  inference is a permanent source of edge cases (`design.md §22`).
- **Lane hashing is sensitive to system-prompt normalization bugs.** If normalization is unstable —
  whitespace, ordering, an injected timestamp — the lane hash churns, every call looks like a new
  lane, and cache stickiness silently evaporates. Normalization needs its own property tests.
- **A failure after stream commitment must fail the request cleanly rather than recovering.** The
  commit barrier trades away a class of automatic recovery in exchange for never emitting a corrupt
  stream. Users will occasionally see a hard error where a spliced retry would have looked
  successful.
- Four identities mean four pieces of state to track, expire, and reconcile per session, plus the
  fingerprint sequence needed for prefix-discontinuity detection.

## Alternatives Considered

- **Free per-call routing** — rejected. Selecting a model independently for every model call
  maximizes per-call optimality but **destroys prompt caches, produces inconsistent tool behavior,
  and destabilizes the agent trajectory** (`design.md §6`).
- **Routing only at session start, with no re-evaluation** — rejected. It is maximally cache-friendly
  but **cannot adapt when a long session shifts task type**, and **cannot respond to a candidate
  becoming ineligible or unhealthy** mid-session. The switching matrix explicitly requires
  re-evaluation at new turns and a switch at the next safe boundary when a candidate becomes
  hard-ineligible (`design.md §8.1`).
- **Mid-stream failover with output splicing** — rejected. Continuing an in-flight response with a
  second model produces **duplicate text, corrupt tool-call JSON, conflicting tool identifiers,
  inconsistent reasoning, and untraceable billing** (`design.md §8.2`).
