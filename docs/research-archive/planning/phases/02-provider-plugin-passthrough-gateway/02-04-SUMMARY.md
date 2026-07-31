# Plan 02-04 Summary — Gateway Core (ingress, relay, upstream, app, health)

**Status**: Complete
**Wave**: 2 · **Agent**: engineering-backend-architect (+ testing-api-tester concerns)
**Requirements**: R3, R5
**Verification**: 24 run / 24 passed, plus 8 stronger replacements and 2 live end-to-end checks
**Tests**: 483 → 620 passing (63 from this plan)

## Files (9, exactly the declared set)

`gateway/{errors,upstream,ingress,relay,app,main}.py`, `health/probe.py`,
`tests/contract/test_passthrough_identity.py` (58 tests),
`tests/performance/test_validation_cost.py` (5 tests).

## Starlette 1.3.1 — the pre-build gate found two real API deltas

The plan text was written against the 0.3x API. Reading the installed source first:

1. **`Starlette.__init__` no longer accepts `on_startup`/`on_shutdown`** (`applications.py:22-29`) — only
   `lifespan`. The plan said "build every validator and the `UpstreamClient` in a startup handler."
   Implemented as an explicit `@contextlib.asynccontextmanager`; `Router.__init__:585` also *warns* on
   a bare async-generator lifespan, so the decorator form is required, not stylistic.
2. **`StreamingResponse` branches on ASGI `spec_version`** (`responses.py:265`). uvicorn 0.51
   advertises `2.3`, so disconnects travel the task-group + `listen_for_disconnect` path. `uvicorn.Server`
   in 0.51 uses a `capture_signals()` context manager rather than `install_signal_handlers()` — two
   servers in one process nest their captures and only the inner survives, so `main.py` subclasses it
   for the admin listener.

`httpx 0.28.1` also checked: `client.stream()` is an `@asynccontextmanager` that cannot return headers
before its first yield, confirming the plan's `(status, headers, iterator)` shape.

## Bug found and fixed — client disconnect did not release the upstream

Starlette's `StreamingResponse` never calls `aclose()` on its `body_iterator`. When a client hangs up
while data flows, the relay generators are suspended **at a `yield`**, so no `CancelledError` is
delivered to them at all — the task dies and the httpx response stays open until GC reaches it.
**On a paid endpoint that is an abandoned generation still billing.**

Fixed with `_RelayResponse`, which closes the relay and then the upstream iterator beneath it,
scheduled via `create_task` (synchronous, so the cancel scope that just fired cannot cancel the
cleanup itself). `test_client_disconnect_releases_the_upstream_stream` asserts the `finally` ran *and*
that the source emitted far fewer chunks than a completed stream would — the second assertion is what
proves the test exercised cancellation rather than a stream that simply finished.

## The measurement that settles `gateway.strict_validation`

Hoisted `openai-chat-request.v1`, 155 KB body / 161 messages, 250 iterations:

| | mean | median | p95 |
|---|---|---|---|
| hoisted request schema | **29.06 ms** | 28.99 ms | 31.79 ms |
| hoisted metadata envelope | **41.2 µs** | 37.3 µs | 50.4 µs |
| unhoisted `validate()` | 69.10 ms | 68.95 ms | 73.44 ms |

`02-CONTEXT.md`'s flip condition was "under 2 ms". Measured cost is **29 ms — 14× over**, and would
consume a third of the entire 100 ms p99 TTFT budget alone. The `check_schema` delta (39.7 ms) is real
and confirms hoisting works, but hoisting does not rescue this. **Keep `strict_validation` off.**
The envelope check at 41 µs is comfortably hot-path-safe. This closes the Phase 3 open item.

## Decisions a reviewer should know

1. **`/healthz` reports the process's own `instance_id`, not the runtime file's.** The plan said to read
   it from the file — that would *defeat* 02-07's PID-reuse defence, since every process sharing a
   state dir would echo the file's value and the "mismatch → another process owns the port" branch
   would be unreachable. `main.py` mints one id and passes it to both `create_app` and `write_runtime`:
   equal by construction when healthy, unequal exactly in the case the check exists for.
2. **The `session_id`-absent risk is handled gateway-side; the fix belongs to 02-05.** An envelope with
   `root_session_id: ""` fails `minLength: 1` → clean 400 in the error envelope, upstream untouched,
   raw id never echoed. An envelope absent entirely is served normally (absent ≠ corrupt). Both pinned.
3. **The body-size cap is a `create_app` parameter, not config.** `GatewayConfig` freezes its key set and
   `config.py` is forbidden to this plan, so "config-overridable" as worded is currently impossible.
4. **`content-length` and `content-encoding` are relayed, not filtered.** A chunked upstream sends no
   content-length, so keeping it preserves what the caller would have seen directly and makes a
   truncated relay surface as the protocol error it is. `RESPONSE_DROP_HEADERS` is hop-by-hop only.
5. **`admin_port: 0` is the sentinel when `admin.py` is absent.** Recording a port with nothing
   listening would send every `stop()` to a closed socket and read as a hung shutdown.
6. **`/healthz` and `/readyz` are unauthenticated**; `/v1/models` requires the token. 02-07 probes
   health on a port before it knows whose it is.

## Vacuous verifications found — 3 of 16

- **T1.5** `'Timeout(' in s` passes on `httpx.Timeout(5.0)` — a *bounded* read timeout, exactly what the
  check exists to forbid. Demonstrated. Replaced with assertions on the constructed client:
  `read is None, connect 5.0, write 10.0, pool 5.0`.
- **T2.4** is `(A and B) or C` where `C` is `A` — passes on source containing `build_validator` *and* a
  forbidden per-request `validate()`. Demonstrated. Replaced with AST proof that `schemas.validate` is
  never imported, no bare `validate()` call exists, and `envelope_validator()` returns the identical
  object twice (6.8 ms first build → 1.6 µs cached).
- **T1.4 / PV5** `'aiter_raw' in s` is satisfied by a docstring. Replaced with AST proof that
  `aiter_raw` is *called* in `upstream.py` and that no `aiter_lines`/`aiter_text`/`aiter_bytes`/
  `decode`/`split` attribute is referenced in `relay.py`.

Also proven beyond the listed commands: `main.py` runs end-to-end (runtime file written **after** bind,
cleared on exit); the loopback gate rejects `0.0.0.0`, `192.168.1.10`, `example.com`, `[::]`
behaviourally; the redaction sink turns `root_session_id` into `root_session_hash` on the app's real
record shape, and no banned key reaches any log call across all 7 modules.

## Issues raised (real, out of scope here)

- **`gateway.max_body_bytes` has no config key.** Whoever next owns `config.py` should add it to
  `_GATEWAY_KEYS`; until then the 32 MiB default is only overridable programmatically.
- **Task 3's own verification list omits the client-disconnect and hop-by-hop cases** that its
  `<action>` block requires. Tests were written for both; a reader trusting only the `> verification:`
  lines would not have caught them.
- **02-02's flagged `error.code` integer risk is live.** A relay emitting an integer `code` passes
  through the gateway untouched (correct) but would fail `assert_error_body` in a contract test.
  Belongs to 02-08's corpus.
- **`_RelayResponse` is a workaround for upstream Starlette behaviour.** If a future Starlette closes
  `body_iterator` itself this becomes redundant — harmless, since closing is idempotent, but worth a
  note at the next dependency bump.
- `tests/performance/test_validation_cost.py` takes ~11 s and is marked `performance` but not `slow`.
