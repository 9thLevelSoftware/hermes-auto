# Plan 01-03 Summary — Wire Protocol Schemas & Schema Loader

**Status**: Complete
**Executed**: 2026-07-26
**Phase**: 01-contracts-schemas-baselines, Wave 2
**Requirements**: R26 (primary), R5 (machine-checkable surface)
**Interpreter**: `C:\Users\dasbl\hermes-auto\.venv\Scripts\python.exe` (Python 3.11.15)
**design.md blob at execution**: `18bb54b36485fa0813ec67f84a74628a9eee3aae` — matches the `01-CONTEXT.md` pin, so every `§N` citation below is valid as read.

---

## The Three Schema `$id` Values

| File | `$id` |
|------|-------|
| `wire/openai-chat-request.v1.schema.json` | `https://hermes-auto-router.dev/schema/wire/openai-chat-request.v1.json` |
| `wire/openai-chat-response.v1.schema.json` | `https://hermes-auto-router.dev/schema/wire/openai-chat-response.v1.json` |
| `wire/sse-stream-contract.v1.schema.json` | `https://hermes-auto-router.dev/schema/wire/sse-stream-contract.v1.json` |

All three declare `"$schema": "https://json-schema.org/draft/2020-12/schema"`, a `title`, a `description`, and `"type": "object"`. All three pass `Draft202012Validator.check_schema()` — they are meta-valid, not merely parseable.

---

## OpenAI Request Fields Deliberately Excluded from the Typed Subset

The request schema sets `"additionalProperties": true` at the top level, so every field below is still **accepted and forwardable** — it is simply not strictly typed. This is deliberate: design.md §5.3 requires the gateway to stay OpenAI-compatible, and a gateway that rejects a field it could have relayed is worse than one that passes it through untyped.

| Excluded field | Why |
|---|---|
| `n` | Multi-completion sampling. The router commits to one route per turn (design.md §8.2); `n > 1` has no defined interaction with the commit barrier, and typing it would imply support the gateway does not yet have. |
| `presence_penalty`, `frequency_penalty`, `logit_bias` | Pure sampling knobs. They carry no routing signal — no bearing on capability, cost, cache, or eligibility — so typing them adds schema surface with no downstream consumer. |
| `logprobs`, `top_logprobs` | Provider support is inconsistent; typing them would suggest the gateway normalizes them across adapters, which is Phase 2+ work. |
| `parallel_tool_calls` | A provider-side toggle. Parallel tool calls are already expressible in the *message* shape (an assistant message with two `tool_calls` entries), which is what design.md §20.3 actually requires be testable. |
| `functions`, `function_call` | Deprecated pre-`tools` API. Typing both generations would force this schema to pick between two incompatible OpenAI API eras — exactly the decision `<stop_gates>` says not to guess at. Left untyped so legacy clients still pass through. |
| `service_tier`, `store`, `metadata`, `reasoning_effort`, `modalities`, `audio`, `prediction` | Newer or vendor-scoped fields with no Phase 1 consumer. They are named explicitly in the schema `description` so a later plan knows the omission was a decision, not an oversight. |

`_hermes_auto` is permitted but **not required**, and typed only as `{"type": "object"}`. Its inner shape belongs to `routing/hermes-auto-metadata.v1.schema.json` (plan 01-04); duplicating it here would create two sources of truth. The schema `description` records that the gateway strips this key before forwarding upstream, per design.md §5.1.

---

## design.md §20.3 Cases: Coverage and Gaps

**Expressible and covered by these three schemas** (each has a passing contract test or a fixture that exercises it):

| §20.3 case | Where |
|---|---|
| Non-streaming text | response schema; `test_valid_response_passes` |
| Streaming text | SSE schema; `test_every_stream_chunk_validates` |
| Single and parallel tool calls | request + response `tool_calls` arrays; `test_valid_request_with_tools_passes` asserts 2 calls |
| Fragmented tool-call arguments | `toolCallDelta.index` as assembly key; `test_fragmented_tool_arguments_concatenate_to_valid_json` |
| Empty deltas | `delta` declares **no** required properties; `test_empty_delta_chunk_is_valid` |
| Usage in final stream chunks | optional top-level `usage` on the chunk; `test_final_stream_chunk_carries_usage` |
| Images | `contentPart` `image_url` branch; asserted in the tools fixture |
| Structured outputs | `response_format` with `type` enum `text`/`json_object`/`json_schema` |
| Refusals | `refusal: string|null` on response and delta messages |

**NOT expressible as a constraint in these three schemas** — recorded here rather than escalated, because the plan enumerated exactly three schemas with fully specified contents and adding a fourth would violate `files_modified`:

1. **Context errors.** An over-context request returns the OpenAI *error envelope* (`{"error": {"message", "type", "code", "param"}}`), which is a different response body than `chat.completion`. No error-envelope schema exists in this plan's file list. **This is a real gap in the frozen contract surface** — a later plan or Phase 2 should add `wire/openai-error.v1.schema.json`. Until it does, error bodies are unvalidated.
2. **Cancellation** and **client disconnect.** Connection-lifecycle events. They have no JSON body at all, so no schema can constrain them; they are behavioral tests against a running gateway (Phase 2+).
3. **Malformed SSE.** Self-defeating as a schema constraint: a JSON Schema can only describe JSON that already parsed. Malformed-SSE handling is parser-level behavior. Related and worth flagging — the stream terminator `data: [DONE]` is **not JSON** and is therefore explicitly out of scope of `sse-stream-contract.v1`, which validates the payload of a single `data:` event only. This is stated in the schema's own `description` so no future implementer tries to validate the sentinel.
4. **Provider reasoning fields — partial.** `completion_tokens_details.reasoning_tokens` *is* typed (it is load-bearing for design.md §7.5 cost reconciliation). But provider-specific reasoning *content* fields (e.g. a `reasoning` or `reasoning_content` string on a message or delta) vary per provider and are left to `additionalProperties: true`. Normalizing them is an adapter concern, not a wire-contract one.

None of these required a decision the plan left unspecified, so no `BLOCKED` was warranted.

---

## The Loader

`src/hermes_auto/gateway/schemas.py` exports exactly four public names via `__all__`: `SCHEMA_ROOT`, `SchemaLoadError`, `load_schemas`, `validate`.

Discovery is by **recursive glob** `**/*.schema.json`, never a hardcoded filename list. That is the mechanism that let plan 01-04 add four routing schemas in parallel with this plan without editing a single file this plan owns — verified: `load_schemas()` against the full root now returns all seven, with zero changes to `schemas.py`.

`sorted()` is applied to the glob so load order — and therefore which file of a duplicate pair is named as the original — is deterministic and error messages are reproducible.

**Error contract.** All four failure modes surface as `SchemaLoadError`, never a bare exception:

| Condition | Behavior |
|---|---|
| Unparseable JSON | `SchemaLoadError` naming the file and the underlying decode error (not `json.JSONDecodeError`) |
| Top-level JSON is not an object | `SchemaLoadError` naming the file **and the type found**. Without the explicit `isinstance` guard, `"$id" not in [1,2,3]` evaluates cleanly and the later key access raises `TypeError`, breaking the stated contract |
| Missing or empty `$id` | `SchemaLoadError` naming the file |
| Duplicate `$id` | `SchemaLoadError` naming **both** file paths |
| Missing / non-existent root | Returns `{}` — deliberately **not** an error, so an optional directory a later phase has not created yet is not a failure |
| Unreadable file (`OSError`) | `SchemaLoadError` naming the file (added beyond spec; same rationale as the others) |

No caching, no `lru_cache`, no module-level singleton — later phases load schemas from temp directories in tests, and a cached global would silently return the packaged set instead.

`validate(instance, schema_id, schemas=None)` raises `KeyError` listing available ids for an unknown `schema_id`, and lets `jsonschema.ValidationError` propagate unchanged so callers keep the full error path.

---

## Files Created

**Schemas**
- `src/hermes_auto/data/schema/__init__.py` — one-line docstring; data only, no logic
- `src/hermes_auto/data/schema/wire/openai-chat-request.v1.schema.json` — request subset: 5-role messages, multipart text/image content, assistant `tool_calls`, `role=tool` results, tool definitions, `tool_choice`, streaming flags, `response_format`, opaque `_hermes_auto`
- `src/hermes_auto/data/schema/wire/openai-chat-response.v1.schema.json` — non-streaming response with `usage.prompt_tokens_details.cached_tokens` and `usage.completion_tokens_details.reasoning_tokens`
- `src/hermes_auto/data/schema/wire/sse-stream-contract.v1.schema.json` — one streamed chunk; documents both design.md §8.2 invariants in its `description`

**Loader**
- `src/hermes_auto/gateway/schemas.py` — glob discovery, `$id` keying, `SchemaLoadError`, `validate()`

**Tests**
- `tests/contract/test_wire_schemas.py` — 9 tests, all `pytest.mark.contract`
- `tests/contract/test_schema_loader.py` — 9 tests, all `pytest.mark.contract`

**Fixtures** (all synthetic — no real prompts, keys, or provider model IDs; placeholder names `test-model-a` / `test-model-b`)
- `tests/fixtures/wire/valid-chat-request.json`
- `tests/fixtures/wire/valid-chat-request-tools.json`
- `tests/fixtures/wire/valid-chat-response.json`
- `tests/fixtures/wire/valid-stream-chunks.json`
- `tests/fixtures/wire/invalid-chat-request-missing-model.json`

---

## Verification Record

All 7 plan-level `verification_commands` exit 0. All 23 in-task `> verification:` gates exit 0. Zero failures, zero retries, zero fixes required.

```
.venv/Scripts/python.exe -m pytest tests/contract/test_wire_schemas.py \
    tests/contract/test_schema_loader.py -q
18 passed in 1.81s
```

Additional evidence gathered beyond the required gates:

- **Meta-validity.** All three schemas pass `Draft202012Validator.check_schema()`. A schema can be valid JSON with a `$schema` key and still be a broken schema; this rules that out.
- **`design.md` unchanged.** `git diff --exit-code design.md` → 0, and the blob still hashes to the `01-CONTEXT.md` pin.
- **Nothing written outside `files_modified`.** `git status --porcelain -uall` shows only this plan's 11 files plus untracked files belonging to plans 01-04 (`data/schema/routing/`, `tests/fixtures/routing/`), 01-05 (`.github/workflows/`, `compatibility.py`, `tests/unit/test_compatibility.py`), and 01-06 (`docs/privacy.md`, `docs/threat-model.md`). None of those were touched by this plan.
- **Forbidden files clean.** `tests/conftest.py`, `tests/contract/__init__.py`, `pyproject.toml`, `src/hermes_auto/version.py` all unmodified.

### Wave-2 scoping honored

No gate in this plan called bare `load_schemas()` — every call scoped to `SCHEMA_ROOT / "wire"`. No gate ran `pytest tests/contract/`; both pytest invocations named this plan's two files explicitly. The test modules themselves also scope to `wire/`, so they stay green regardless of what other schema directories contain.

---

## Packaging Risk Investigated and Cleared

Creating `src/hermes_auto/data/schema/__init__.py` turns `hermes_auto.data.schema` into a real package. That could plausibly have re-attributed the `.schema.json` files away from the `hermes_auto` package, breaking `pyproject.toml`'s `data/schema/**/*.json` `package-data` glob — the exact silent packaging failure plan 01-01's summary warned about, and one that would leave every in-repo `PYTHONPATH=src` gate green while `load_schemas()` returned `{}` from an installed copy.

Probed empirically in a throwaway copy outside the repository (`pyproject.toml` is frozen and was not touched):

```
schema json in wheel: ['hermes_auto/data/schema/routing/hermes-auto-metadata.v1.schema.json',
                       'hermes_auto/data/schema/routing/model-card.v1.schema.json',
                       'hermes_auto/data/schema/routing/outcome-event.v1.schema.json',
                       'hermes_auto/data/schema/routing/route-decision.v1.schema.json',
                       'hermes_auto/data/schema/wire/openai-chat-request.v1.schema.json',
                       'hermes_auto/data/schema/wire/openai-chat-response.v1.schema.json',
                       'hermes_auto/data/schema/wire/sse-stream-contract.v1.schema.json']
schema pkg init in wheel: ['hermes_auto/data/schema/__init__.py']
```

**All seven schemas ship. No `pyproject.toml` change is needed.** The phase-close wheel gate asserting `len(n) == 7` should pass as written. The scratch copy was deleted. (This snapshot happened to include plan 01-04's four routing schemas, already on disk at probe time — so the 7-count is real, not extrapolated. The gate should still be run for record after all plans land.)

---

## Notes for Downstream Plans

1. **Plan 01-07** validates against 01-04's outcome-event schema using this loader. Use:
   ```python
   from hermes_auto.gateway.schemas import SCHEMA_ROOT, load_schemas, validate
   schemas = load_schemas(SCHEMA_ROOT / "routing")   # or bare load_schemas() post-phase
   validate(event, "<outcome-event $id from 01-04>", schemas)
   ```
   Pass the loaded mapping as the third argument in loops — `validate()` re-reads every file from disk when `schemas` is omitted.
2. **`validate()` raises `KeyError`, not `ValidationError`, for an unknown `$id`.** Two different failure modes; catch them separately.
3. **`load_schemas()` returns `{}` for a missing root rather than raising.** A caller that expects schemas must assert on the result — an empty mapping is silence, not an error.
4. **No error-envelope schema exists.** Anything validating a gateway *error* response has nothing to validate against yet. See §20.3 gap 1 above.
5. **`data: [DONE]` is out of scope** of the SSE schema. Strip the sentinel before validating chunks.
6. A stale, gitignored `build/` directory exists at the repo root from another plan's wheel build. It contains none of this plan's artifacts and was left untouched.
