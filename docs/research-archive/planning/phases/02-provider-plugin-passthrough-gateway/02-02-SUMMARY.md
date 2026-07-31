# Plan 02-02 Summary — Error Schema, Async Test Mode, Redaction & Salt

**Status**: Complete with Warnings
**Wave**: 1 · **Agent**: engineering-senior-developer (+ security-engineer concerns)
**Requirements**: R3, R5
**Verification**: 24 run / 23 passed

## Files

**Created**: `src/hermes_auto/data/schema/wire/openai-error.v1.schema.json`,
`src/hermes_auto/telemetry/redaction.py`, `tests/contract/test_error_envelope_schema.py` (23 tests),
`tests/unit/test_redaction.py` (51 tests).

**Modified**: `pyproject.toml` (3 runtime deps, 1 dev dep, `asyncio_mode`, 4 markers);
`gateway/schemas.py` (**exactly one line** — the new `$id`);
`tests/contract/test_schema_loader.py` (**exactly one line** — see Decisions).

## Contracts frozen here

- **New schema `$id`**: `https://hermes-auto-router.dev/schema/wire/openai-error.v1.json` — 8 schemas total
- **Salt file**: `~/.hermes/auto-router/salt`, 64 hex chars, single non-inherited ACE
- **Banned keys `redact()` enforces** — exact name, case-folded. Plans 02-04 and 02-07 must not log on
  any of these: `messages`, `content`, `prompt`, `tool_calls`, `tool_result`, `tool_output`,
  `arguments`, `authorization`, `api_key`, `token`, `secret`.
  `root_session_id`/`session_id` → `root_session_hash` (HMAC-SHA256, 64 lowercase hex).
- **Resolved deps**: `starlette 1.3.1`, `uvicorn 0.51.0`, `httpx 0.28.1`, `pytest-httpx 0.36.2`.
  `fastapi`, `pydantic`, `psutil`, `structlog` absent and asserted absent.

## The failed verification is a guard defect

VC7 forbids all of `src/hermes_auto/gateway` — but the plan's own `execution_contract` authorizes
editing `EXPECTED_SCHEMA_IDS` in `gateway/schemas.py` as "the one edit you may make to that file."
**It would fail with no siblings running at all.** The remaining entries are plan 02-01's declared
deliverables. Nothing was reverted. Three replacement guards were run and pass: routing schemas and
the three existing wire schemas unchanged; `git diff -U0` on `schemas.py` yields exactly one changed
content line and it is the new `$id`; `design.md` still at blob `18bb54b3`.

## Decisions a reviewer should know

1. **The plan's claim that the schema count "moves from 7 to 8 in one place" was wrong.** A second
   hardcoded `7` lived in `tests/contract/test_schema_loader.py:325`. There is no way to satisfy both
   `load_schemas() == 8` and `pytest tests/contract -q` exit 0 without changing it. One integer
   changed; `tests/contract/` is not forbidden. CI is unaffected — `ci.yml` derives every count from
   `len(EXPECTED_SCHEMA_IDS)` and hardcodes 7 only in comments.
2. **`install_salt() -> str` returns hex, not bytes** — the plan's typed signature said `str`, its
   prose said it returns the raw bytes. Resolved to the signature: 32 random bytes, stored and
   returned as 64 lowercase hex, `cat`-inspectable; HMAC uses `bytes.fromhex()` internally.
3. **`session_digest`/`redact` take a keyword-only `directory=None`.** Both remain callable exactly as
   the frozen signature specifies. This is the minimum needed to make "two salts over one session id
   diverge" testable without a test-only global-reset backdoor.
4. **Banned keys match by exact name, not substring.** A substring rule on `token`/`prompt` would
   delete `token_count`, `prompt_tokens`, `completion_tokens` — the derived counts `docs/privacy.md`
   says the router legitimately keeps. Pinned by `test_derived_token_counts_survive`. Stated
   limitation: `user_prompt` is *not* matched.
5. **When the salt is unavailable, `redact()` drops the session id entirely** — it does not raise
   (logging must not cause outages) and does not emit an unsalted digest (invisible in output, since
   unsalted sha256 also matches `^[0-9a-f]{64}$`). Dropping is the only remaining option.
6. **`icacls` failure deletes the salt file and raises `RedactionError`.** A salt that exists but is
   world-readable is worse than none — the next call would read it back and trust it.
7. **`get_logger` sets `propagate = False`.** A propagating logger hands the *unredacted* record to
   root's handlers, which have their own formatters.

## Vacuous verifications found and strengthened

- **`assert 'hmac' in inspect.getsource(m)` is satisfied by the `import hmac` line alone.**
  Demonstrated empirically: a module reading `import hmac` + `hashlib.sha256(SALT + sid.encode())`
  passes while being exactly the unsalted-concatenation construction the plan forbids. Replaced with
  a test asserting `hmac.new(` present **and** `hashlib.sha256(` absent, plus one that recomputes the
  HMAC independently and asserts it differs from the naive concatenation.
- **`assert d['additionalProperties'] is True` checks top level only** — a schema closing the inner
  `error` object would pass. Added assertions at both levels plus a positive relay test.
- **`asyncio_mode` check only proves a string is in a TOML file.** Confirmed load-bearing by creating
  a temporary async test: passes under `asyncio_mode=auto`, fails under `-o asyncio_mode=strict`
  ("async def functions are not natively supported"), then deleted.
- **Wheel gate added (not in the plan).** Built twice and inspected: 8 `.schema.json` entries
  including the new one, plus `telemetry/redaction.py`. The existing `data/schema/**/*.json` glob
  already covers it — **no packaging change was needed**, confirmed rather than assumed.
- **Windows ACL readback proved non-vacuous** by comparison against a control file in the same
  directory: control shows 6 inherited ACEs, salt shows 1 non-inherited. The test's assertion fails
  on the control.

## Issues raised (real, out of scope here)

- **`starlette>=0.37` resolved to `1.3.1`** — a major version past what the floor implies. Plan 02-04
  must verify its ASGI / `StreamingResponse` API against Starlette 1.x, not the 0.3x docs.
- **`error.code` is typed `["string","null"]` per the plan.** Some OpenAI-compatible relays emit an
  integer `code` (HTTP status). Under `gateway.strict_validation: true` that body would fail
  validation on a relay schema whose stated purpose is never to reject forwardable traffic — the same
  class as the `contentPart` defect Phase 1 found. Flagged for 02-08's fixture corpus.
- **`test_descriptions_make_no_absolute_containment_claim` covers routing schemas only**, not all
  eight — contrary to what `CODEBASE.md` claimed. The `_OVERCLAIMS` list was reproduced into
  `test_error_envelope_schema.py` so the new schema is genuinely subject to the house rule. Whether
  the walker should widen to all 8 belongs to whoever owns `test_routing_schemas.py`.
- `02-CONTEXT.md` says `.planning/CODEBASE.md` "does not exist." It does, and it is accurate.
