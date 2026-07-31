# Plan 01-02 Summary — Architecture Decision Records

**Phase**: 01-contracts-schemas-baselines
**Plan**: 02 (Wave 1)
**Status**: Complete with Warnings
**Date**: 2026-07-26
**Requirement**: R26 (frozen public contracts before routing logic)

## Files created

| Path | Purpose |
|------|---------|
| `docs/adr/README.md` | ADR convention, five-section template, numbering rules, index of all five records |
| `docs/adr/0001-provider-plugin-and-local-gateway.md` | Integration-boundary decision |
| `docs/adr/0002-session-and-cache-switching-policy.md` | Route identity and switching rules |
| `docs/adr/0003-model-card-architecture.md` | Candidate identity and metadata precedence |
| `docs/adr/0004-local-telemetry-and-privacy.md` | Telemetry posture and privacy defaults |
| `docs/adr/0005-adapter-isolation-and-rollout-tiers.md` | Adapter tiering and dependency isolation |
| `docs/architecture.md` | System overview indexing all five records |

`docs/` did not previously exist; this plan created it, as assigned.

## Final ADR titles and numbers

| ADR | Title | Status | design.md sources cited |
|-----|-------|--------|-------------------------|
| 0001 | Route behind the Hermes provider boundary, not inside `AIAgent` | Accepted — 2026-07-26 | §1, §3, §5.1, §5.2, §5.3, §22 |
| 0002 | Route on cache boundaries using a four-part route identity | Accepted — 2026-07-26 | §6, §6.1–§6.4, §8.1, §8.2, §22 |
| 0003 | Candidates are identity tuples described by versioned model cards | Accepted — 2026-07-26 | §7.5, §9.1, §9.2, §9.3, §10.3 |
| 0004 | Local-first telemetry with no raw prompt retention | Accepted — 2026-07-26 | §13.1, §13.2, §13.3, §13.4, §13.5 |
| 0005 | Four-tier adapter rollout with an isolated sidecar environment | Accepted — 2026-07-26 | §10.2, §10.3 |

Every ADR uses the five required H2 headings in order (`## Status`, `## Context`, `## Decision`,
`## Consequences`, `## Alternatives Considered`) with both `### Positive` and `### Negative`
subsections. Verified mechanically.

## design.md pin

Verified before writing: `git rev-parse HEAD:design.md` =
`18bb54b36485fa0813ec67f84a74628a9eee3aae`, matching the blob pinned in `01-CONTEXT.md`. All `§N`
citations therefore refer to the intended sections. `git diff --exit-code design.md` passes.

## Ambiguities encountered and how they were resolved

### 1. "Unknown pricing is never zero" is in §7.5, not §9.x

The plan directed ADR-0003 to state that unknown pricing is never represented as zero, citing
`§9.1`, `§9.2`, and `§9.3`. Those sections do not contain that rule. It appears verbatim in
**§7.5 Step 5: Calculate expected cost** ("Never represented as zero cost", alongside "Allowed in
`quality` only when the user permits unknown-cost models" and "Excluded from `economy` by default").

**Resolution**: recorded the rule in ADR-0003 as instructed but cited it to `design.md §7.5`, its
actual source, rather than to a section that does not support it. Not treated as BLOCKED — the
decision exists in design.md and is unambiguous; only the plan's citation pointer was off by a
section. The §9.x citations remain on the candidate-identity and model-card-schema content they do
support.

### 2. §9.3 states "four layers" but gives a six-level precedence chain

§9.3 opens with "Merge metadata from four layers" (operator policy overrides, measured Hermes
performance, curated capability and harness profiles, registry defaults), then immediately gives an
explicit precedence block with **six** levels — adding *live health* and *conservative unknown
defaults*.

**Resolution**: not a genuine conflict. The prose lists the four *authored* metadata sources; the
precedence block orders all six *resolution* levels including runtime health and the fallback floor.
The explicit precedence block is the operative specification and is reproduced verbatim and in order
in ADR-0003. Both readings are consistent; no BLOCKED emitted.

### 3. Task 2's `<done>` field says "all five design.md §3 alternatives"

The task's `<action>` body says four rejected rows and explicitly warns that the fifth row
("register a custom provider pointing at a local gateway") is the *Recommended* row and is the
Decision, not an alternative. The `<done>` line says "all five".

**Resolution**: followed the `<action>` body and the execution-brief restatement. ADR-0001 lists
**four** rejected alternatives. The recommended row is the `## Decision`. No fifth alternative was
invented. The `python` alternatives-count check asserts `n >= 4` and returns exactly 4.

### 4. `must_haves` wanted the literal `docs/adr/0001` in architecture.md, but working links are `adr/…`

`docs/architecture.md` lives inside `docs/`, so functioning relative links to the records are
`adr/0001-….md`, which does not contain the literal substring `docs/adr/0001` that the plan's
`must_haves.contains` requires.

**Resolution**: kept the working relative links **and** added an explicit repo-relative path list
under "Decision records" for citation from plans and code comments. Both forms now present; links
still resolve on disk (verified by `test -f` on each target).

## Decisions the phase success criteria required but design.md did not support

**None.** All five records required by the Phase 1 success criterion — provider-plus-gateway
integration, session/cache switching policy, model-card architecture, local telemetry and privacy,
and adapter isolation — are fully supported by design.md. No decision was invented.

## Scope discipline

- **Routing algorithm internals were deliberately excluded.** No scoring formula, dimension weight
  value, hysteresis threshold, or circuit-breaker constant appears in any ADR. `design.md §7.4`,
  `§7.7`, `§7.8`, and `§7.9` were consulted only to confirm what belongs to Phase 4 rather than
  here. `docs/architecture.md` states this exclusion explicitly so a later reader does not mistake
  the omission for an oversight.
- **`docs/threat-model.md`, `docs/privacy.md`, and `docs/evaluation.md` were NOT created.** They are
  owned by plans 01-06 and 01-07. `docs/architecture.md` links them as forward references annotated
  "Created in a later plan of this phase". Those links are intentional and must not be "fixed".
- **Nothing under `src/`, `tests/`, `.github/`, `scripts/`, `pyproject.toml`, `README.md`, or
  `design.md` was created or modified** by this plan.

## Verification

23 verification commands run across the three tasks plus the plan-level list; 22 exited 0.

**One failure, diagnosed as a cross-plan false positive:**

```
git status --porcelain -- docs/threat-model.md docs/privacy.md docs/evaluation.md SECURITY.md README.md | grep -q . && exit 1 || exit 0
```

Output:

```
?? README.md
?? SECURITY.md
```

`README.md` and `SECURITY.md` are untracked files created by **plan 01-01**, which runs in parallel
in Wave 1 and owns the repo meta files. This plan neither created nor modified them; both are in
this plan's `files_forbidden`. This is exactly the scenario `01-CONTEXT.md` § Phase-Close Gate
anticipates: "A plan asserting on the whole tree would fail on another plan's in-progress work and
emit a false `BLOCKED`."

No fix was applied, because the only "fix" would be deleting another plan's owned files — a
forbidden action. The assertion was instead re-run scoped to this plan's actual ownership:

```
$ git status --porcelain -- docs/threat-model.md docs/privacy.md docs/evaluation.md
(empty)
$ for f in docs/threat-model.md docs/privacy.md docs/evaluation.md; do test -e "$f" ...
absent  docs/threat-model.md
absent  docs/privacy.md
absent  docs/evaluation.md
```

The substantive assertion — that this plan did not create the three later-plan documents — passes.

### Final checklist results

| Check | Result |
|-------|--------|
| Exactly five numbered ADRs | 5 |
| All five H2 sections + Positive/Negative in every ADR | pass |
| Every ADR cites `design.md §N` | 5 of 5 |
| ADR-0001 names `AIAgent` mutation and the fork-native SPI as rejected | pass |
| ADR-0001 alternatives count | 4 (>= 4 required) |
| ADR-0003 six-level chain, verified in strict order | pass |
| ADR-0004 four prohibitions + opt-in export | pass |
| ADR-0005 LiteLLM as transport, never the decision engine | pass |
| architecture.md links all five ADRs, relative + repo-relative | pass |
| No scoring formulas / weight values / thresholds in any ADR | pass |
| `docs/threat-model.md`, `privacy.md`, `evaluation.md` not created | pass |
| `.github/` untouched | pass |
| `git diff --exit-code design.md` | pass |

## Handoff

### Plan 01-04 (decision, model-card and outcome-event schemas)

From **ADR-0003**, when writing the model-card JSON Schema:

- The card's identity is the **seven-field tuple**: provider, endpoint, model, credential scope,
  protocol adapter, harness profile, harness version. The schema must be able to express two cards
  for the same base model reached through different providers as distinct candidates. A schema keyed
  on model ID alone contradicts this record.
- Card blocks are `transport`, `capabilities`, `affinities`, `limits`, `economics`, `harness`,
  `policy`, `evidence` (`design.md §9.2`).
- **`economics` prices must be nullable, and null must be distinguishable from `0`.** Unknown
  pricing is never zero. Do not give price fields a `default: 0`; a missing price is an explicit
  unknown handled by mode policy.
- `harness.version` is part of candidate identity, so it is required, not optional.
- The `evidence` block carries `source_version`, `calibration_dataset`, `sample_count` — it is what
  lets a decision record which card version produced it, so `RouteDecision` should be able to carry
  a card-version reference.
- The six-level precedence chain (hard operator policy > live health > measured candidate
  statistics > curated model card > registry metadata > conservative unknown defaults) is a
  **merge-time** contract, not a schema constraint. The schema describes the *curated model card*
  layer only. Do not attempt to encode precedence in the schema.

From **ADR-0004**, when writing the outcome-event schema:

- The schema must make raw content **unrepresentable**, not merely discouraged. No field may accept
  prompt text, tool-result bodies, or secrets. Prefer `additionalProperties: false` on event objects
  so a future field cannot smuggle content in.
- Session identifiers in events are **salted hashes**, not raw Hermes session IDs. Type them as
  opaque hash strings.
- Permitted content is derived features and numeric scores: token counts, capability estimates,
  domain tags, costs, latencies, reason codes.
- The ten tables in `design.md §13.1` are the event families the schema must cover:
  `route_decisions`, `route_attempts`, `usage_events`, `health_observations`, `turn_outcomes`,
  `session_outcomes`, `model_cards`, `model_card_versions`, `router_versions`, `feedback`.
- Export is opt-in and exporters may emit only derived routing features, candidate IDs, decisions,
  usage, and policy-approved outcomes — so if the schema has an export-shape variant, it is a
  **subset**, never a superset.

Also relevant: **ADR-0002** defines the four-part route identity (root session, lane, user turn,
cache epoch). `RouteDecision` and outcome events should be keyed by that identity, and lane IDs are
hashes, not raw values.

### Plan 01-06 (threat model and privacy documentation)

From **ADR-0004**:

- The **four privacy prohibitions** are hard defaults that hold with zero configuration, not
  configuration recommendations. State them in the user-facing guide in exactly this force: no raw
  prompt text, no tool-result bodies, no secrets, no default external export.
- Storage is **SQLite in WAL mode**, one local file, inspectable offline with standard tooling —
  this is what makes the privacy claim verifiable rather than merely asserted, and is worth saying
  to users.
- Root session identifiers are **hashed with a local salt**. The privacy consequence worth
  documenting: cross-machine correlation is impossible **by design**, not merely disabled.
- Local **deletion and retention controls** are a promised capability; the privacy guide should
  describe how a user exercises them.
- **External export is opt-in per Hermes contribution policy** (`design.md §13.1`), and even when
  enabled the exporter emits only derived routing features, candidate IDs, decisions, usage, and
  policy-approved outcomes (`design.md §13.5`).
- Optional **code outcomes** (`design.md §13.4`) are a second, narrower opt-in scoped to a
  repository the user explicitly trusts, and remain local unless separately exported. Document it as
  distinct from telemetry export.
- Training corpora for the learning phases need their **own separate opt-in** — the default store is
  deliberately too thin to train on, and this must not be presented as covered by the telemetry
  default.
- `docs/architecture.md` already links `docs/privacy.md` and `docs/threat-model.md` as forward
  references. Creating those files resolves the links; no edit to `architecture.md` is required, and
  the "Related documentation" annotations may be updated by 01-06 if desired.

### Plan 01-03 / 01-05 / 01-07 (informational)

- **ADR-0001** fixes the `_hermes_auto` envelope as the metadata channel (protocol version, root
  session ID, virtual model, plugin version) and requires the gateway to **strip it before sending
  upstream** — relevant to 01-03's wire-protocol schemas.
- **ADR-0005** fixes the Tier 4 credential bridge as version-gated and fail-closed with a mandatory
  startup compatibility probe and **nightly CI against Hermes `main`** — this is the architectural
  justification for 01-05's compatibility probe and nightly workflow.
