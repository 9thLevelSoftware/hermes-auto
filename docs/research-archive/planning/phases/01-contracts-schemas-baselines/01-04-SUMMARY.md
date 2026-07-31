# Plan 01-04 Summary — Decision, Model-Card & Outcome-Event Schemas

**Phase**: 01-contracts-schemas-baselines
**Plan**: 04 (Wave 2)
**Status**: Complete
**Date**: 2026-07-26
**Requirements**: R26 (primary), plus the schema-only surfaces of R1, R6, R14, R19

`design.md` blob verified against the `01-CONTEXT.md` pin
(`18bb54b36485fa0813ec67f84a74628a9eee3aae`) **before** writing. All `§N` citations below therefore
refer to the intended sections. `git diff --exit-code design.md` exits 0.

---

## The four schema `$id` values

| File | `$id` |
|------|-------|
| `src/hermes_auto/data/schema/routing/hermes-auto-metadata.v1.schema.json` | `https://hermes-auto-router.dev/schema/routing/hermes-auto-metadata.v1.json` |
| `src/hermes_auto/data/schema/routing/route-decision.v1.schema.json` | `https://hermes-auto-router.dev/schema/routing/route-decision.v1.json` |
| `src/hermes_auto/data/schema/routing/model-card.v1.schema.json` | `https://hermes-auto-router.dev/schema/routing/model-card.v1.json` |
| `src/hermes_auto/data/schema/routing/outcome-event.v1.schema.json` | `https://hermes-auto-router.dev/schema/routing/outcome-event.v1.json` |

All four declare `"$schema": "https://json-schema.org/draft/2020-12/schema"`, a `title`, a
`description`, and `"additionalProperties": false` at top level. All four pass
`Draft202012Validator.check_schema`.

---

## ADR ↔ design.md divergences and how each was resolved

### 1. ADR-0003's seven-element candidate identity vs. `transport.required`

**Divergence.** ADR-0003 records candidate identity as the tuple *(provider, endpoint, model,
credential scope, protocol adapter, harness profile, harness version)*. `design.md §9.2`'s card
example carries only `adapter`, `provider`, `model`, `credential_ref` under `transport`, and its
`harness` block is a separate optional section.

**Resolution — no BLOCKED.** These describe different things and therefore do not conflict. The
seven-element tuple is the **runtime composition of identity performed in Phase 3** from the card
plus resolved operator configuration; it is not the set of fields a card must restate.
`transport.required` is exactly `["adapter", "provider", "model"]`. `base_url` and `credential_ref`
are optional, because endpoint and credential scope are frequently supplied by configuration rather
than per card, and a local unauthenticated candidate has no credential at all. This reasoning is
recorded in the `transport` block's own `description` so a later reader does not "fix" it, and
`test_transport_requires_exactly_three_fields` asserts the exact three-element set.

### 2. "Unknown pricing is never zero" lives in §7.5, not §9.x

As plan 01-02 already discovered, the rule the plan attributes to the model-card sections appears
verbatim in **`design.md §7.5` Step 5** ("Never represented as zero cost", alongside "Allowed in
`quality` only when the user permits unknown-cost models" and "Excluded from `economy` by default").
The `economics` block's `description` cites `§7.5` — its actual source — rather than a §9.x section
that does not support it. Every price field is `{"type": ["number", "null"], "minimum": 0}` and
**carries no `default`**, so a missing price remains an explicit unknown for mode policy to handle
rather than being filled in by the validator.

### 3. `harness.version` — "required" in ADR-0003, block optional in design.md §9.2

**Divergence.** ADR-0003's handoff states "`harness.version` is part of candidate identity, so it is
required, not optional." The plan lists `harness` itself as an optional block.

**Resolution — no BLOCKED.** Both hold simultaneously. The `harness` **block** is optional (a card
may decline to declare a harness profile), but *when the block is present* `version` is required:
`harness.required == ["version"]`. `version` is an `integer, minimum 1` rather than a free-form
string, per ADR-0003's requirement that it increment so measurements taken under an old harness are
invalidated by a total, machine-checkable ordering.

### 4. `design.md §12`'s example emits `DEBUGGING_AFFINITY`

The §12 example's `reason_codes` array contains `DEBUGGING_AFFINITY`, which is **not** among the
twelve codes the same section freezes. The enum was **not** widened; the fixture uses
`["CAPABILITY_FIT", "DOMAIN_AFFINITY", "COST_TIEBREAK"]`. `test_reason_code_enum_has_twelve_codes`
asserts `len(enum) == 12`, asserts exact set equality, and explicitly asserts
`"DEBUGGING_AFFINITY" not in enum`. `grep -q 'DEBUGGING_AFFINITY' tests/fixtures/routing/valid-route-decision.json`
finds nothing.

### 5. No genuine conflict was found

No field is addressed by both an ADR and design.md in mutually exclusive terms. No `BLOCKED` was
warranted on stop-gate grounds 3.

---

## design.md §13.2 / §13.3 outcome fields dropped or reshaped for privacy

The outcome-event property set is a **closed allowlist** of 23 properties. `additionalProperties` is
`false` at every object level (verified programmatically by
`test_outcome_event_defines_no_content_carrying_property`, which walks the schema tree). None of
`prompt`, `prompt_text`, `raw_prompt`, `messages`, `content`, `tool_result`, `tool_output`,
`response_text`, `api_key`, `secret`, or `credential` exists at any depth.

**Reshaped:**

| design.md source | Field as written | Reshaping and why |
|---|---|---|
| §13.2 "API success or failure" | `event_type` enum + `turn_succeeded` | Carried as event family and a boolean; no status body, no provider message. |
| §13.2 "Tool-call count", "Invalid tool-call count" | `tool_call_count`, `invalid_tool_call_count` | **Counts only.** Tool names, arguments, and results are unrepresentable. |
| §13.2 "Actual input/output/cache/reasoning tokens" | `input_tokens`, `output_tokens`, `cached_tokens`, `reasoning_tokens` | **Counts only.** Reasoning tokens are counted, never captured — §12 forbids exposing hidden model reasoning. |
| §13.2 "Empty response" | `empty_response` (boolean) | A flag, never the response body. |
| §13.2 "Candidate fallback" | `event_type: "fallback_used"` + `candidate` | Expressed as an event family rather than a free-form field. |
| §13.3 "Tool execution failures" | `event_type: "tool_error"` + `error_class` | `error_class` is a router-assigned **classification** (`rate_limit`, `auth_failure`, …), never a captured error body, provider message, or stack trace. It carries `maxLength: 64` as a deliberate second barrier against a future implementation pasting a message body into it. |
| §13.3 "Explicit positive or negative feedback" | `feedback` enum `good` \| `bad` | **Deliberately a two-valued enum, not free text.** Free-text feedback would be a content-retention channel that ADR-0004 forbids. |
| §13.2 "Actual cost" | `actual_cost_usd`, nullable | Null means unknown (e.g. an unpriced local candidate) and is never reconciled as zero, consistent with §7.5. |

**Dropped from v1 (not representable in the frozen allowlist):**

| design.md source | Item | Note |
|---|---|---|
| §13.3 | "Test command results" | Closer to §13.4 optional code outcomes, which are a separate, narrower opt-in. Not in v1. |
| §13.3 | "Whether the user immediately requested a correction" | Not in the plan's frozen allowlist. Partially inferable from `feedback: "bad"` plus turn adjacency. |
| §13.3 | "Whether the user retried or changed models" | Partially inferable from `retry_count` and successive `decision_id` / `candidate` pairs on the same `lane_id`. |
| §13.3 | "Whether the session progressed to a different task" | Not in v1; lane and cache-epoch transitions are the nearest available signal. |

None of these were dropped because they would leak content — each is a boolean or categorical that
could be added safely. They are simply outside the property list this plan was directed to freeze.
Phase 8 should add them via a `v2` schema rather than by loosening `v1`.

**Known gap, flagged for Phase 8 (not a defect in this plan's scope):** `event_type` admits
`health_observation`, but the frozen allowlist contains no numeric field to carry the observed health
score. A `health_observation` event is therefore currently well-formed but empty of observation. The
plan's field list was explicit and exhaustive, so no field was invented; Phase 8 should add a
`health` (`number`, 0–1) property in a `v2` bump. Recorded here rather than silently patched.

---

## Files created (exactly the 12 in `files_modified`, nothing else)

**Schemas**

| Path | Purpose |
|------|---------|
| `src/hermes_auto/data/schema/routing/hermes-auto-metadata.v1.schema.json` | The `_hermes_auto` envelope injected by `build_extra_body()`. `protocol_version` `const: 1`; closed object so a version skew fails loudly. Description states the gateway MUST strip the key before forwarding upstream and that a non-1 version MUST be rejected, not best-effort parsed. |
| `src/hermes_auto/data/schema/routing/route-decision.v1.schema.json` | The explainability record. Hashed session identity, sparse eight-dimension requirement vector, non-empty exclusion reasons, nullable `selected`, exactly twelve reason codes, optional `model_card_version` / `router_version` for replay. |
| `src/hermes_auto/data/schema/routing/model-card.v1.schema.json` | Candidate card: `transport`, `capabilities` (all eight required, 0–1), `affinities` (ten closed domain tags), `limits`, `economics` (nullable), `harness`, `policy`, `evidence`, `context_anchor`. |
| `src/hermes_auto/data/schema/routing/outcome-event.v1.schema.json` | Telemetry event. Closed 23-property allowlist of identifiers, numeric measurements, and small enums. |

**Tests and fixtures**

`tests/contract/test_routing_schemas.py` (19 tests, all `pytest.mark.contract`) plus seven synthetic
fixtures under `tests/fixtures/routing/`: `valid-hermes-auto-metadata.json`,
`valid-route-decision.json`, `valid-model-card.json`, `valid-model-card-unknown-pricing.json`,
`valid-outcome-events.json`, `invalid-model-card-capability-out-of-range.json`,
`invalid-outcome-event-raw-prompt.json`.

All fixture content is synthetic: candidate ids are `test-local-fast`, `test-hosted-general`,
`test-coding-specialist`; no real provider model IDs; no credential values; `credential_ref` is
`env:TEST_PROVIDER_KEY` or `none`; hostnames are `.invalid` or loopback.

---

## Verification record

**35 verification commands run; 35 exited 0; 0 failed. No fix attempts were needed.**

| Gate group | Result |
|---|---|
| Task 1 (`> verification:` lines) | 7/7 pass |
| Task 2 (`> verification:` lines) | 9/9 pass |
| Task 3 (`> verification:` lines) | 10/10 pass |
| Plan-level `verification_commands` | 9/9 pass |

Notable outputs:

```
$ ./.venv/Scripts/python.exe -m pytest tests/contract/test_routing_schemas.py -q
19 passed in 0.99s

$ PYTHONPATH=src ./.venv/Scripts/python.exe -c "... load_schemas(SCHEMA_ROOT/'routing') ..."
loader ok, 4 schemas

$ grep -riE '"(prompt_text|raw_prompt|messages|tool_result|api_key|secret)"' \
    src/hermes_auto/data/schema/routing/outcome-event.v1.schema.json
(no match — exit 1, gate inverts to PASS)

$ git diff --exit-code design.md
design.md UNCHANGED (rc=0)
```

Additional checks beyond the required gates:

- All four schemas pass `jsonschema.Draft202012Validator.check_schema`.
- A programmatic tree walk confirms the outcome-event schema is closed at every object level and
  contains no banned property name at any depth.
- `credential_ref`'s pattern was exercised directly: accepts `env:TEST_PROVIDER_KEY` and `none`,
  rejects `sk-test-not-a-real-key` and `env:lowercase`.
- `economics` price fields carry no `default` key.

### Wave-2 scoping discipline (honored)

- **No gate called bare `load_schemas()`.** Every loader gate passes `SCHEMA_ROOT / "routing"` and
  asserts exactly 4.
- **`pytest tests/contract/` was never run.** Only `tests/contract/test_routing_schemas.py`.
- **`src/hermes_auto/gateway/schemas.py` was read, never edited.** It globs
  `**/*.schema.json` recursively and keys by `$id`, so these four files are purely additive. No edit
  was needed and none was made.
- `src/hermes_auto/data/schema/wire/`, `tests/contract/test_wire_schemas.py`,
  `tests/contract/test_schema_loader.py`, `tests/contract/__init__.py`, `tests/conftest.py`,
  `pyproject.toml`, `docs/`, `.github/`, `scripts/`, and `design.md` were **not** created or modified
  by this plan. `git status --short -uall` confirms this plan's footprint is exactly its twelve
  `files_modified` paths.

At the time of this plan's completion, `gateway/schemas.py` plus three wire schemas from 01-03 were
present on disk, so the phase-close `len(load_schemas()) == 7` assertion is consistent
(3 wire + 4 routing). That assertion belongs to the phase-close gate and was deliberately not run
here.

### Timing note

`src/hermes_auto/gateway/schemas.py` did not exist when this plan began. Schema authoring proceeded
first, the loader gate was retried afterward, and 01-03 had landed the module by then. No `BLOCKED`
was warranted.

---

## Handoff

### Plan 01-07 (baseline corpus & measurement harness)

01-07 wraps each event as `{"task_id": ..., "event": {...}}` and validates the **inner** object
against the outcome-event schema. Concretely:

- **Schema `$id` to validate against**:
  `https://hermes-auto-router.dev/schema/routing/outcome-event.v1.json`
- **Load it without touching the wire schemas**:
  ```python
  from hermes_auto.gateway.schemas import SCHEMA_ROOT, load_schemas, validate
  schemas = load_schemas(SCHEMA_ROOT / "routing")   # exactly 4
  validate(record["event"], "https://hermes-auto-router.dev/schema/routing/outcome-event.v1.json", schemas)
  ```
  `validate(instance, schema_id, schemas)` raises `jsonschema.ValidationError` on failure and
  `KeyError` on an unknown `$id`. Pass the already-loaded mapping — the loader does not cache, so
  omitting it re-reads every file on every call.
- **The wrapper key `task_id` must live OUTSIDE the event object.** The outcome-event schema is
  `additionalProperties: false`, so putting `task_id` *inside* the event will fail validation. This
  is intentional, not an oversight — validate `record["event"]`, never `record`.
- **Five fields are required on every event**: `event_id`, `event_type`, `occurred_at` (RFC 3339),
  `root_session_hash`, `lane_id`. Everything else is optional, so a sparse baseline event is legal.
- **`event_type` is one of**: `request_completed`, `request_failed`, `turn_completed`, `tool_error`,
  `user_feedback`, `health_observation`, `fallback_used`.
- **`decision_id` must match `^dec_`** if present. `feedback` is `good` or `bad` only.
  `error_class` is `maxLength: 64` — a classification token, never a message body.
- **`actual_cost_usd` is nullable.** A recorded run against an unpriced local candidate should emit
  `null`, not `0`. Baseline cost aggregation must therefore treat `null` as *unknown* and exclude it
  from means rather than summing it as zero — otherwise the baseline report will understate cost for
  exactly the candidates it knows least about.
- **`format: "date-time"` is not enforced** by `jsonschema` without a format checker installed. If
  01-07 wants `occurred_at` strictly validated, pass a
  `jsonschema.FormatChecker()`; otherwise a malformed timestamp will pass. The same applies to
  `format: "uri"` on `transport.base_url`.
- **Baseline corpora must contain no raw prompt text** (`01-CONTEXT.md` constraint 4). The schema
  enforces this for events; corpus task definitions are 01-07's own responsibility.
- **Determinism**: nothing in these schemas introduces ordering or volatile fields. Event ordering in
  a fixture array is 01-07's to make stable.

### Phases 3, 4, and 8 (informational)

- **Phase 3** (model-card loading): cards are untrusted input — validate on load against
  `model-card.v1.json`. The six-level precedence chain is a merge-time contract and is deliberately
  **not** encoded in the schema; the schema describes the *curated model card* layer only.
- **Phase 4** (decision generation): `selected` is nullable and is **not** constrained to equal
  `ranked[0].candidate` — a health veto depends on that freedom, and
  `test_route_decision_selected_need_not_be_top_ranked` will fail if it is ever tightened. An empty
  `ranked` with a populated `excluded` is a valid structured failure. `shortfall` has a floor of 0
  and **no ceiling**, because it sums positive per-dimension deficits and can exceed 1.
- **Phase 8** (SQLite telemetry store): the outcome-event allowlist is the schema-level contract for
  the store's columns. Widening it requires a `v2` `$id`, not an edit to `v1`. See the
  `health_observation` gap noted above.

---

## Issues

1. **`health_observation` events carry no health value in v1.** Flagged above; deliberate
   plan-fidelity choice, resolve in a Phase 8 `v2` bump.
2. **Four `design.md §13.3` turn-level signals are not representable in v1** (test command results,
   immediate correction request, retry/model change, session task progression). Listed above with
   the nearest available proxy for each. All are privacy-safe and could be added in `v2`.
3. **`format` keywords are advisory.** `date-time` and `uri` are not enforced by default in
   `jsonschema`. Noted in the 01-07 handoff.

## Errors

None.
