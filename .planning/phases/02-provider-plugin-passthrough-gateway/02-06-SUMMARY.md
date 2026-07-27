# Plan 02-06 Summary — Admin API (separate listener, separate auth scope)

**Status**: Complete
**Wave**: 3 · **Agent**: engineering-backend-architect (+ security-engineer concerns)
**Requirements**: R3, R4
**Verification**: 20 run / 20 passed
**Tests**: 622 → 693 passing (71 from this plan)

## Files (3, exactly the declared set)

`gateway/admin.py` (690 lines), `gateway/admin_main.py` (181 lines),
`tests/contract/test_admin_api.py` (1251 lines, 71 tests).

**`main.py` did not need editing** — plan 02-04 already wired `_build_admin_app` to lazily
`from .admin import create_admin_app`. Creating that symbol turns the wiring on. No scope conflict.

## The critique's #1 finding is closed, and proven closed

The original plan set had the admin listener **started by nothing**: `stop()`'s documented "primary path
on every platform" POSTed to a port with no listener and fell through to a hard kill, and the runtime
contract had no `admin_port` to find it with.

`test_the_recorded_admin_port_names_a_listener_that_is_accepting` runs a real subprocess with
**ephemeral ports** — so the recorded port is a value the test did not choose — then reads the runtime
file and connects:

| Check | Result |
|---|---|
| runtime file `admin_port` | `60426` — not the `0` sentinel, and `!= port` |
| admin port, `/admin/v1/status` + admin token | 200, `instance_id` matches the runtime file |
| admin port, same request + **inference** token | 401 |
| admin port, `/healthz` | 401 (admin app, not the inference app) |
| inference port, `/healthz` | 200 |
| inference port, `/admin/v1/status` + admin token | 404 — surfaces are distinct |
| `POST /admin/v1/shutdown` | 202, `servers_signalled: 2`, exit 0, runtime file cleared |

**Proven non-vacuous**: with `create_admin_app` renamed, the same subprocess records `admin_port: 0` —
the pre-plan state reproduced, then closed.

## Vacuous verifications: 9 of 20, demonstrated by construction

The agent built a deliberately wrong module — `require_admin` as `supplied == expected`, no routes, no
file writes — whose docstring mentions the right words. It **passes** V3/F4 (`'compare_token' in s`),
V4 (`'icacls' in s or 'auth' in s` — `'auth'` matches any docstring saying "authentication"),
V7 (`'501'` and `'not_implemented' in s`), V8 (`'redact' in s`), V11/F5 (`'127.0.0.1' in s`). Also:

- **V2** `assert read_admin_token() != read_token()` passed in a state dir where the inference token was
  never minted — `str != None`. Satisfied by an implementation that never writes an admin token.
- **V12** `grep -q 'inference token' <testfile>` passed on a file with one comment and zero tests.

Replacements are AST- and behaviour-based: `compare_token` proven *called* and `hmac.compare_digest`
proven *not* called locally; `read_token`/`mint_token` proven unreachable from `admin.py` (blocking a
"fall back to the inference token" refactor); permissions by real ACL readback; 501s by real responses
validated against `openai-error.v1`; loopback by reading the **bound socket address** back.

## Mutation testing on its own suite

| Mutation | Caught by |
|---|---|
| admin accepts the inference token as fallback | 10 tests, incl. all 6 parametrised scope tests + e2e |
| `AdminAuthMiddleware` removed | 32 tests |
| `force_exit = True` added to shutdown | the in-flight-stream drain test, alone |
| shutdown signals no servers | drain test + e2e |
| empty-expected-token guard disabled | **initially missed** — the agent's own test was vacuous; rewritten to inject `""` at the branch, now caught |
| `create_admin_app` renamed | subprocess records `admin_port: 0` |

## Decisions a reviewer should know

1. **`create_admin_app`'s lifespan mints the admin token if absent.** The plan's edge case (missing token
   file → 401, listener still starts) is preserved and tested by deleting the file at runtime — the
   token is read **per request**, not cached. Never minting it would leave every supervised gateway
   401-ing its own shutdown endpoint, making graceful drain unreachable on the platform with no
   `SIGTERM` — reintroducing the defect this plan closes. Mirrors `app.py`.
2. **`compare_digest(b"", b"") is True`**, so `require_admin` branches explicitly on an absent expected
   token and denies unconditionally, still paying a decoy comparison so absent and wrong do not
   separate under timing.
3. **Auth is middleware, not per-handler** — a route appended to a live app is protected without an auth
   call. Unknown paths return 401, not 404.
4. **Shutdown reaches uvicorn by frame introspection** (`asyncio.all_tasks()` → `serve()`'s
   `cr_frame.f_locals["self"]`). No ASGI API exposes the `Server`, and `main.py` was not this plan's to
   edit. `signal.raise_signal(SIGINT)` was rejected: under any host that installed no handler
   (TestClient) it converts a graceful stop into a crash. An explicit `request_exit=` hook exists;
   `servers_signalled` is reported so a caller can see a count of 0.
5. **`/admin/v1/status` reads `instance_id` from the runtime file** — deliberately unlike `/healthz`,
   which reports the answering process's own id so 02-07's PID-reuse mismatch branch stays reachable.
6. **`admin_port` added to the status payload** (additive); `upstream_base_url` has userinfo stripped
   before `redact()`.

## No CORS — now asserted

`02-CONTEXT.md` noted this was asserted nowhere. Added: the admin app's middleware list is exactly
`[AdminAuthMiddleware]`; a cross-origin request emits no `Access-Control-*` header; a preflight is not
answered; and the inference app is asserted CORS-free too (read-only, `app.py` untouched).
**A control test builds a genuinely CORS-enabled app and requires the headers to appear** — otherwise
the negative tests would prove nothing.

## Contracts recorded

- **Admin token**: `<state_dir>/admin-token`, `secrets.token_urlsafe(32)` (43 chars), written with
  `auth.secure_write`, readback via `admin_token_permissions_ok()`
- **Admin port**: `config.gateway.admin_port` (default `port + 1`); `main.py` binds and records the
  **bound** port; `admin_main.py` is debug-only and writes no runtime file
- **Graceful drain**: `should_exit = True` on every uvicorn server; `force_exit` never set;
  `timeout_graceful_shutdown` is `None` (asserted), so in-flight streams complete
- **501 body**: `{"error": {"message": "... is not implemented in Phase 2 ... delivered in Phase N ...",
  "type": "not_implemented", "param": null, "code": "not_implemented"}}`. Messages use the route
  **template**, never the caller's `session_id`/`decision_id` — no reflection primitive on an endpoint
  with no other behaviour. `decisions`→Phase 4, `reroute`→Phase 5, `pin`→Phase 3, `feedback`→Phase 8.

## Issues raised

1. **`gateway/ingress.py:195` has a fail-open auth path.** `compare_token(supplied, expected_token or "")`
   returns **True** when the app has no token and the caller sends no header, because
   `compare_digest(b"", b"")` is `True`. The comment directly above states the intent the code violates:
   *"a gateway with no token configured must reject every request."* Not reachable in production —
   `app.py`'s lifespan always mints — but `create_app()` without a lifespan authenticates
   unauthenticated requests. **Authorized to plan 02-07 as an explicit scope extension.**
2. **Nothing mints the admin token outside the gateway's own lifespan.** 02-07's `setup`/`doctor` should
   mint and permission-check it so `stop` works against a gateway started before this landed.
3. **`starlette.testclient` warns its `httpx` backend is deprecated** (`install httpx2`). Harmless today;
   it will bite at the next Starlette bump, and `pyproject.toml` pins lower bounds only.
4. **`_running_uvicorn_servers()` depends on `uvicorn.Server.serve` remaining the outermost coroutine of
   its task.** Stable across 0.51 and covered by the drain test, but it is the one place a uvicorn
   upgrade could silently degrade shutdown to `servers_signalled: 0` — which the response body at least
   reports honestly.
5. **`mint_admin_token()` in V1/V2 wrote a real `admin-token`** into the developer's state directory, as
   the plan's commands specify. Same class of artifact 02-01 noted for the inference token.
