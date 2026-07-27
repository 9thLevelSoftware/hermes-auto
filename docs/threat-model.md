# Threat Model

STRIDE threat model for the Hermes Auto Router. Its authoritative source is the fourteen-row threat
and mitigation table in `design.md` §21; every row of that table appears here exactly once, with its
required mitigation preserved, a STRIDE category, an owning module, and the phase that implements
the mitigation.

Companion documents:

- [`SECURITY.md`](../SECURITY.md) — reporting process and the four published security invariants.
- [`docs/adr/0001-provider-plugin-and-local-gateway.md`](adr/0001-provider-plugin-and-local-gateway.md)
  — the loopback-binding and sidecar trust boundary.
- [`docs/adr/0004-local-telemetry-and-privacy.md`](adr/0004-local-telemetry-and-privacy.md) — the
  telemetry and privacy decision record.
- [`docs/privacy.md`](privacy.md) — the same posture written for users rather than reviewers.

**Phase numbering.** Phase numbers in this document follow the project roadmap, in which Phase 1 is
the current contracts-and-schemas phase. The roadmap is offset by one from `design.md` §18, which
numbers the same sequence from Phase 0. Where a mitigation is already implemented, the row says so.

---

## Scope and trust boundaries

The router is a supervised sidecar process. Hermes believes it is talking to one stable provider and
one virtual model; the sidecar performs the real candidate selection and calls the real provider
(ADR-0001). That indirection creates four trust boundaries.

**Boundary 1 — Hermes process to gateway sidecar.** Hermes reaches the gateway over loopback HTTP on
an OpenAI-compatible Chat Completions surface (`design.md` §5.3). The request carries a `_hermes_auto`
metadata envelope and is authenticated with a bearer token generated at setup time. Everything on the
Hermes side of this boundary is a separate OS process that the router does not control. The gateway
treats every request as untrusted input regardless of which local process sent it, and strips the
internal routing metadata before anything goes upstream (`design.md` §5.4).

**Boundary 2 — gateway to upstream provider.** The gateway crosses the public internet holding
provider credentials, and it sends prompt and code content to whichever candidate it selected. This is
the boundary with the largest blast radius: it is the only one where user content leaves the machine,
and it is the only one where a long-lived secret is presented to a party outside the user's control.

**Boundary 3 — gateway to local telemetry store.** A single local SQLite file in WAL mode under the
router state directory (`design.md` §13.1, §16). The gateway writes derived routing evidence; readers
— the control plugin, `/auto stats`, and the admin API — read concurrently. The boundary matters
because it is where a defect turns transient request content into durable stored content.

**Boundary 4 — the admin API.** `/admin/v1/*` exposes status, decision inspection, reroute, pin, and
feedback. Per `design.md` §5.3 it uses a **different authentication scope** from the inference API and
preferably a **separate local listener**. It is a distinct boundary because it is the only surface
that can change routing behavior at runtime, so an inference-scope token must not reach it.

### Out of scope

- **Hermes's own security posture.** The router does not audit, harden, or vouch for the Hermes Agent
  process, its plugins, or its configuration. It assumes Hermes is trusted-but-untrustworthy input:
  authenticated, but never believed about its content.
- **Upstream provider security.** Provider-side breach, retention, or misuse of a prompt sent to a
  selected candidate is outside the router's control. The router's obligation is to route only where
  policy permits, not to secure the destination.
- **The operating-system user account** running the sidecar. Token files and the SQLite store are
  protected by file permissions; an attacker who already holds that account's privileges has already
  won, and no control listed here restores the boundary.

---

## Assets

What an attacker wants, roughly in descending order of value:

| Asset | Why it is valuable | Where it lives |
|---|---|---|
| Provider API credentials | Direct, billable access to paid frontier models under the user's account | Environment variables, referenced from config as `env:VAR_NAME` and never by value |
| Generated local bearer token | Grants a local process the ability to submit requests as Hermes, spending the user's provider budget | Token file with owner-only permissions under the router state directory |
| Prompt and code content in flight | Source code, internal architecture, unreleased plans, and secrets pasted into prompts | Gateway memory only, for the duration of a request |
| Local telemetry store | A durable record of routing behavior, cost, and work patterns; the highest-value target that persists | One SQLite file under the router state directory |
| Routing policy configuration | Control over which candidates are eligible and which cost ceilings apply; rewriting it redirects every future turn | `config.yaml` |
| Model cards | Capability, limit, and price metadata that drives selection; falsified cards steer routing without touching policy | Packaged defaults plus user overrides and registry snapshots |

---

## STRIDE summary

How each STRIDE category applies to this system.

| Category | How it applies |
|---|---|
| **Spoofing** | Any local process able to reach the gateway port can present itself as Hermes and spend the user's provider budget; the bearer token, not the source address, is what distinguishes callers. |
| **Tampering** | Model metadata, price data, classifier inputs, the streamed response, and shared session state are all attacker-influenceable, and each one can corrupt a routing decision or its output without touching a credential. |
| **Repudiation** | Which candidate actually served a turn is knowable only from the decision record; if a model response could write into that record, cost attribution and every explanation built on it become unprovable. |
| **Information disclosure** | The sidecar sits in the most sensitive data path a developer has, so the dangerous failures are the quiet ones — a credential in a log line, or request content that becomes durable storage. |
| **Denial of service** | Money, not uptime, is the scarce resource: an attacker who drives escalation to the most expensive tier exhausts a budget far faster than one who exhausts a socket. |
| **Elevation of privilege** | Two paths grant authority that was never delegated — user text reaching the policy layer, and third-party code executing inside a process that holds every provider credential. |

---

## Threats and mitigations

One row per row of `design.md` §21, in source order. Required mitigation text is preserved verbatim;
parenthetical notes name secondary modules. Most owning modules do not exist yet — naming them is the
point, so that no mitigation can be silently dropped when its phase is planned. Module paths are
relative to `src/hermes_auto/` and are drawn from `design.md` §17.

| Threat | STRIDE | Mitigation | Owning module | Phase |
|---|---|---|---|---|
| Prompt causes expensive escalation | Denial of service | Hard cost ceiling, bounded requirement scores, escalation tier cap, anomaly detection (score bounds in `routing/requirements.py`, tier cap in `routing/policy.py`, anomaly detection in `health/anomalies.py`) | `routing/cost.py` | Phase 4; anomaly detection Phase 6 |
| Prompt attempts to modify router policy | Elevation of privilege | Policy never derived from user text; no prompt-triggered configuration commands (policy read from `config.py` only; `gateway/ingress.py` removes internal routing metadata before upstream per §5.4) | `routing/policy.py` | Phase 4; ingress stripping Phase 2 |
| Local port accessed by another process | Spoofing | Loopback binding, generated bearer token, no CORS, restrictive token-file permissions (binding in `gateway/app.py`; separate admin auth scope in `gateway/admin.py` per §5.3) | `gateway/auth.py` | Phase 2 |
| Secret leakage in logs | Information disclosure | Central redaction, credential references rather than values, structured logging allowlist (`env:VAR_NAME` references resolved in `config.py`; explanations constrained in `routing/explain.py`) | `gateway/errors.py` | Phase 2 |
| Malicious model metadata | Tampering | Schema validation, source checksums, operator overrides, conservative defaults (checksummed registry snapshots in `inventory/snapshots.py`; overrides in `inventory/model_cards.py`) — validation is grounded by the model-card JSON Schema delivered by plan 01-04 in Phase 1 | `inventory/validation.py` | Phase 3; schema Phase 1 |
| Sidecar dependency compromise | Elevation of privilege | Isolated environment, pinned dependencies, lockfile, vulnerability scanning (isolation from the Hermes environment per ADR-0005; lockfile and scanning in CI under `.github/workflows/`; per-adapter isolation in `adapters/`) | `supervisor.py` | Phase 2; CI scanning Phase 1; adapter isolation Phase 7 |
| Raw code exported unintentionally | Information disclosure | No raw storage by default, explicit export consent, local retention controls (event shape fixed by `telemetry/events.py` and the outcome-event schema from plan 01-04; retention window in `telemetry/retention.py`) — see ADR-0004 | `telemetry/exporters.py` | Phase 8; schema Phase 1 |
| Cost estimate manipulation | Tampering | Reconcile predicted and actual usage, cap output, unknown price is never zero (§7.5; reconciliation against observed usage in `telemetry/outcomes.py`) | `routing/cost.py` | Phase 4; reconciliation Phase 8 |
| Router classifier attack | Tampering | Input truncation, score caps, out-of-distribution detection, deterministic fallback (truncation in `routing/features.py`; heuristic fallback in `routing/heuristic_predictor.py`; out-of-distribution scoring in `routing/learned_predictor.py`) | `routing/requirements.py` | Phase 4; out-of-distribution detection Phase 9 |
| Model response spoofs route metadata | Repudiation | Gateway — not the target model — sets decision headers and logs (headers emitted on the response path in `gateway/app.py` and `gateway/streaming.py`; no route metadata is ever read back out of an upstream response) | `routing/decision.py` | Phase 4 |
| Mid-stream fallback corrupts tools | Tampering | First-chunk commit barrier; no post-commit model splicing (§8.2 — the route commits once upstream connects, status is valid, and the first valid SSE event is parsed; fallback selection in `health/tracker.py`) | `gateway/streaming.py` | Phase 6 |
| Concurrent state collision | Tampering | Per-lane locks, transactional state updates, hashed compound keys (lane identity in `state/lanes.py`; cache-epoch keys in `state/cache_epochs.py`) | `state/locks.py` | Phase 5 |
| Experimental model degrades quality | Denial of service | Shadow stage, minimum samples, anomaly circuit breaker, immediate disable (minimum-sample thresholds and half-open probes per §11.1 in `health/tracker.py`; shadow comparison in `evaluation/replay.py`) | `health/circuit_breaker.py` | Phase 6; shadow evaluation Phase 8 |
| Hermes upgrade breaks private bridge | Denial of service | Version gating, compatibility tests, bridge optional and fail-closed (the fail-closed startup probe already exists as `compatibility.py`, delivered by plan 01-05, with nightly CI against Hermes `main` per ADR-0005) | `adapters/hermes_native.py` | Bridge Phase 7; probe implemented in Phase 1 |

### Classification notes

Two assignments are judgment calls and are recorded here rather than left implicit.

- **Sidecar dependency compromise** is classified *Elevation of privilege* rather than *Tampering*.
  The intrusion is a tampering event, but the operative risk is that third-party code executes with
  the sidecar's full authority — every provider credential, the bearer token file, and the telemetry
  store. "Isolated environment" is a blast-radius control, which is what makes the elevation framing
  the more useful one for whoever implements it.
- **Model response spoofs route metadata** is classified *Repudiation* rather than *Spoofing*. The
  required mitigation is an authority-of-record control — the gateway is the sole author of decision
  headers *and logs* — and the harm it prevents is an unprovable answer to "which candidate served
  this turn, and who owes for it". The spoofing aspect is structurally foreclosed rather than
  detected: route metadata is never parsed back out of an upstream response.

---

## Residual risk

What this model does **not** mitigate. These are accepted, not overlooked.

- **A compromised operating-system user account defeats every local control.** The bearer token file
  and the SQLite store are protected by file permissions only. An attacker running as that user reads
  both. Loopback binding stops remote access and other user accounts; it does nothing against the
  account that owns the process.
- **A malicious or compromised upstream provider sees every prompt routed to it.** Routing policy
  chooses destinations; it cannot constrain what a destination does with what it receives. Users who
  need this boundary closed should restrict eligible candidates to local models.
- **The router cannot detect a model that is subtly wrong rather than failing.** Health tracking keys
  on failure rates, empty responses, invalid tool calls, and latency (`design.md` §11.1). A candidate
  that returns confident, well-formed, incorrect output scores as healthy. Shadow staging and outcome
  metrics narrow this but do not close it, and no automated control here substitutes for the user
  noticing.
- **Cost ceilings bound spend per turn, not per session.** Repeated in-bound turns can still
  accumulate an unwelcome total. Session-level budgets are not in the current design; until they
  ship, the anomaly detector and the per-turn ceiling are the only spend controls.
- **The security contact in `SECURITY.md` is a placeholder** pending release. Until it is filled in,
  there is no verified private reporting channel, which is a Phase 11 release-checklist item.
- **Repudiation coverage rests on a single row.** Only the decision record establishes which
  candidate served a turn. If the local store is deleted — which users are explicitly entitled to do
  — that record is gone, and cost attribution for prior turns is unrecoverable. This is a deliberate
  trade in favor of the privacy posture in ADR-0004.
- **Most mitigations in the table are forward references.** As of Phase 1 only the fail-closed
  compatibility probe and the schema-validation groundwork exist. Every other row names an unwritten
  module. The table is a commitment, not a description of the current implementation.

No contradiction was found between `SECURITY.md` and `design.md` §21. The four invariants published
in `SECURITY.md` — loopback-only binding, generated bearer token with restrictive file permissions,
no CORS, and no raw prompt or secret retention by default — restate the mitigations for the *Local
port accessed by another process*, *Secret leakage in logs*, and *Raw code exported unintentionally*
rows, and strengthen them from defaults to hard requirements.

---

## Review cadence

This document is reviewed at **every phase boundary**, before the phase is marked complete.

A phase may not close while any of the following is true:

1. The phase introduced a **new external input** — a new endpoint, a new file read at runtime, a new
   registry or metadata source, a new configuration key influenced by anything outside the user's
   own config — and no row covers it.
2. The phase introduced a **new stored field** in the local event store, and it was not checked
   against the four prohibitions in ADR-0004 and recorded in [`docs/privacy.md`](privacy.md).
3. The phase introduced a **new network destination**, including a new exporter target, and no row
   covers what leaves the machine and under whose consent.
4. The phase implemented a mitigation named in the table above, and the row still reads as a forward
   reference rather than naming the delivered module.

Each review also re-checks that this document and `SECURITY.md` still agree. `SECURITY.md` publishes
the posture; this document explains what it defends against. If they diverge, the divergence is the
finding.
