# Phase 2: Provider Plugin & Passthrough Gateway — Context

## Phase Goal
Prove Hermes can run entirely through the virtual provider with **no observable behavioral difference** against a single fixed target.

This corresponds to `design.md` §18 Phase 1 and PRs 2–4 of the §19 sequence. There is **no routing logic** in this phase — the gateway forwards every request to one configured OpenAI-compatible endpoint. The claim being proven is negative and precise: a fixed candidate behaves identically through the gateway and called directly.

## Requirements Covered

`.planning/REQUIREMENTS.md` does not exist. Descriptions below are from `.planning/PROJECT.md` § Requirements § Active.

- **R1** — `hermes-auto` ProviderProfile registering virtual models `auto:quality`, `auto:balanced`, `auto:economy`, `auto:session`, injecting `_hermes_auto` routing metadata via `build_extra_body()`
- **R2** — `hermes-auto-control` plugin: slash commands, CLI subcommands, lifecycle hooks, sidecar supervision
- **R3** — Local gateway exposing OpenAI-compatible `POST /v1/chat/completions` and `GET /v1/models`, plus `/healthz`, `/readyz`, and a separately-scoped admin API
- **R4** — Loopback binding, generated bearer-token auth, restrictive token-file permissions, process supervision on Windows, macOS, and Linux
- **R5** — Streaming SSE and tool-call behavior identical to talking to the target provider directly

## VERIFIED HERMES FACTS — these correct `design.md`

Read before writing any plugin code. Three architecture-proposal agents independently inspected the local Hermes 0.19.0 checkout at `C:\Users\dasbl\AppData\Local\hermes\hermes-agent`, and the coordinator re-verified the two load-bearing claims directly. **`design.md` §5.1 is illustrative, not literal.**

### 1. Model providers are discovered by directory scan, NOT pip entry points

`providers/__init__.py::_discover_providers()` scans, in precedence order:
1. Bundled: `<hermes-repo>/plugins/model-providers/<name>/`
2. User: `$HERMES_HOME/plugins/model-providers/<name>/`

Loading is via `importlib.util.spec_from_file_location`. User plugins override bundled ones (last writer wins).

**Consequence:** `pip install hermes-auto-router` **will not register the provider.** A `hermes auto setup` command that writes a shim into `$HERMES_HOME/plugins/model-providers/hermes-auto/__init__.py` is load-bearing, not convenience. A plan that assumes pip-install-is-enough produces "unknown provider hermes-auto" at first use.

The entry-point group `hermes_agent.plugins` (confirmed at `hermes_cli/plugins.py:217`) exists for **general** plugins only. R2's control plugin uses it; R1's provider does not.

### 2. `ProviderProfile` is a dataclass — subclass for methods, then instantiate with kwargs

`providers/base.py:38` is `@dataclass class ProviderProfile`. The canonical bundled example (`plugins/model-providers/openrouter/__init__.py`) does:

```python
class OpenRouterProfile(ProviderProfile):   # subclass to override methods
    def build_extra_body(self, *, session_id=None, **context): ...

openrouter = OpenRouterProfile(              # INSTANTIATE with kwargs
    name="openrouter",
    display_name="OpenRouter",
    ...
)
register_provider(openrouter)                # register the INSTANCE
```

`design.md` §5.1 shows bare class attributes on the subclass and never instantiates. Following it literally does not register anything. Use the form above.

### 3. The control plugin must be enabled in Hermes config, or it never loads

`hermes_cli/plugins.py:1453` — entry-point plugins are **opt-in via `plugins.enabled`**. `_get_enabled_plugins()` returns `None` when the key is absent, and `None` means nothing is enabled: the plugin is recorded with `error = "not enabled in config"` and its `register()` is never called.

Consequence: declaring the `hermes_agent.plugins` entry point is **not sufficient**. Without `plugins.enabled` containing `hermes-auto-control`, `hermes auto ...`, `/auto status`, and the session-start hook do not exist — and `hermes auto setup`, the command that installs the provider shim, cannot itself be run, because it is registered by the plugin that is not loaded. `design.md` line 1548 shows `plugins: enabled: - hermes-auto-control`; plan 02-07 must write it or fail loudly with the exact `hermes plugins enable` command.

### 4. Hermes is a source checkout, not an importable distribution

On this machine `./.venv/Scripts/python.exe -c "import providers"` raises `ModuleNotFoundError` and `importlib.metadata.version("hermes-agent")` raises `PackageNotFoundError`. Hermes lives as a checkout at `C:/Users/dasbl/AppData/Local/hermes/hermes-agent` with its own bundled runtime under `.hermes-runtime/`.

So any verification that constructs a real `ProviderProfile` must run **as a subprocess against the Hermes interpreter with `cwd` at the checkout root** — `providers` is a top-level package importable only from there. A plan whose verification command calls `build_profile()` in the sidecar venv cannot pass.

### 5. Other verified facts

| Fact | Value | Source |
|---|---|---|
| `build_extra_body` signature | `(self, *, session_id: str \| None = None, **context: Any) -> dict[str, Any]` | `providers/base.py:119` |
| Real `ProviderProfile` fields | `name`, `api_mode`, `aliases`, `display_name`, `description`, `signup_url`, `env_vars`, `base_url`, `models_url`, `auth_type`, `supports_health_check`, `supports_vision`, … | `providers/base.py:38-75` |
| Hermes's OpenAI client pin | `openai==2.24.0` | Hermes `pyproject.toml:40` |
| Hermes chat transport | **synchronous** `openai.OpenAI` over httpx | agent runtime |
| General-plugin entry group | `hermes_agent.plugins` | `hermes_cli/plugins.py:217` |
| Control-plugin surface | `ctx.register_cli_command`, `ctx.register_command` (slash), `ctx.register_hook` | plugin loader |
| Model providers have slash commands? | **No** — R2 must live in the general plugin | plugin loader |

`supports_health_check` and `models_url` are real fields worth setting deliberately: Hermes's own `doctor` probes `/models` unless `supports_health_check=False`.

## Architecture Decision — Pragmatic, with two elements from Clean

Three read-only proposal agents produced Minimal (zero deps, stdlib `http.server`, sync threads), Clean (Starlette + `BackendAdapter` + all six §5.4 seams), and Pragmatic (Starlette, async, opaque-dict passthrough, no speculative seams).

**Selected: Pragmatic.** The reasoning that decided it:

- **Async is the one genuinely expensive-to-reverse decision.** Phases 4–8 need parallel health probes (§11.1), a first-chunk commit barrier holding a second candidate in flight (§8.2), and many concurrent streaming sessions. Those are cancellation problems; `asyncio.TaskGroup` and `CancelledError` express them natively, threads do not. Reversing sync→async touches every I/O call site and the whole test suite. **Hedge kept: routing and scoring stay pure synchronous functions.** Only ingress, upstream I/O, and supervision are async.
- **Opaque-dict passthrough makes R5 structurally true** rather than test-enforced. Parse → `pop("_hermes_auto")` → re-dump → relay frames verbatim. Unknown fields survive by construction. Retrofitting passthrough onto a typed model is a rewrite; the converse is additive. This is why FastAPI is rejected — its value is Pydantic request modeling, and any field not modeled gets silently dropped, which fails R5.
- **The on-disk supervision contract ships to users' machines** and needs migration code forever if wrong. Freeze it in Wave 1.

**Rejected from Clean: introducing `BackendAdapter` now.** Clean argued Phase 7 otherwise rewrites the request path. But an abstraction validated by exactly one implementation is usually the wrong abstraction, and Phase 7 arrives with three real targets to shape it against. The cost of retrofitting an interface onto one call site is hours; the cost of a wrong Protocol propagated across four adapters is a signature change everywhere. Phase 2 hardcodes the single target behind one `upstream.py` module.

**Adopted from Clean:**
1. **Redaction is a boundary, not a convention.** `telemetry/redaction.py` ships in Phase 2 with a single sink. "No raw prompt content in any log" is cross-cutting — deferring it to Phase 8 means Phases 3–7 each write leaking logs that then need auditing.
2. **Windows ACL readback in `doctor`.** `os.chmod` does not affect Windows ACLs; a `0o600` call silently leaves the token world-readable. `doctor` must read the effective ACL back, not assume the write succeeded.

**Rejected from Minimal: stdlib `http.server`.** Hand-rolled HTTP/1.1 keep-alive and chunked framing is exactly where these gateways break, and the failure mode — the `openai==2.24.0` SDK reconnecting per turn — shows up as a TTFT regression rather than a test failure. That is not less complexity; it is complexity you maintain yourself.

**Dependencies added this phase:** `starlette`, `uvicorn[standard]`, `httpx`. Refused: `pydantic`, `psutil`, `structlog`, `fastapi`.

## What Already Exists (Phase 1)

- `pyproject.toml` — `hermes-auto-router`, src-layout, deps `packaging`, `jsonschema`, `pyyaml`, `referencing`; dev `pytest`, `pytest-asyncio`, `hypothesis`. **No HTTP framework, client, or server yet.**
- `src/hermes_auto/` — eleven subpackages, stubs except `compatibility.py`, `gateway/schemas.py`, `evaluation/*`
- `gateway/schemas.py` — `load_schemas()`, `validate()`, `build_validator()`, `build_registry()`, `EXPECTED_SCHEMA_IDS`. **`validate()` costs ~50 ms/call unhoisted** because it re-runs `check_schema`; `build_validator()` exists so callers hoist that out of hot paths.
- Seven versioned schemas. Three are wire-protocol: `openai-chat-request.v1`, `openai-chat-response.v1`, `sse-stream-contract.v1`.
- `compatibility.py` — fail-closed Hermes probe, `>=0.19,<1.0`
- 206 tests, isolated `.venv` at `<repo>/.venv`, CI with a wheel-packaging gate
- `design.md` pinned at blob `18bb54b36485fa0813ec67f84a74628a9eee3aae`

## Phase 1 Carryovers Assigned Here

From `.planning/STATE.md` § Open Items:

1. **No error-envelope schema.** `wire/openai-error.v1.schema.json` is needed — context errors return the OpenAI error body, a different shape from `chat.completion`, currently unvalidated. Plan 02-02.
2. **`pytest-asyncio` 1.4.0 no longer defaults to a usable mode.** Needs `asyncio_mode` in `[tool.pytest.ini_options]` before the first async test. Plan 02-02. This blocks every async test in the phase, so it lands in Wave 1.
3. **Salting has no structural backstop.** The gateway receives `root_session_id` in the `_hermes_auto` envelope and must never log it raw. Plan 02-02 ships the per-install salt and the test that pins it (same session id under two salts → two digests).
4. **Do not copy plan 01-06's `git diff --quiet` guard idiom** — it cannot detect an untracked forbidden file. Every cross-plan guard in this phase uses `git status --porcelain -- <path> | grep -q . && exit 1 || exit 0`.
5. **Candidate ids now permit `/`**, so `../../etc/passwd` matches the pattern. Any consumer using a candidate id as a path component must sanitize. Relevant if the gateway ever derives a filename from one.

## Phase-Wide Constraints

1. **Sidecar dependencies stay isolated from the Hermes environment.** The provider shim that executes *inside* the Hermes venv must import **stdlib only**. All real logic lives in the isolated sidecar env and is reached over HTTP or a console script.
2. **Loopback-only binding, generated bearer token, no CORS.**
3. **No raw prompts, tool-result bodies, or secrets in any log** — enforced by the redaction sink, not by reviewer memory.
4. **Added TTFT overhead under 1% or 100 ms p99.** A relay that buffers is disqualifying. `aiter_raw`, never `aiter_lines`.
5. **Behavioral settings in `config.yaml`; only credentials and generated tokens in environment variables.**
6. **`design.md` is read-only** and appears in `files_forbidden` for every plan. Verify the pinned blob before relying on a `§N` citation.
7. **Windows is first-class.** No `fork`, no POSIX signals, no process groups as POSIX knows them. PID reuse is fast.
8. **Use `<repo>/.venv/Scripts/python.exe`** for every python/pip/pytest call. The machine default interpreter is the Hermes Agent venv.
9. All verification commands are POSIX shell — run through Bash, never PowerShell.

## Configuration Contract — frozen in Wave 1

`load_config(None)` resolves in this order: `HERMES_AUTO_CONFIG` → `$HERMES_HOME/config.yaml` (reading only the `auto_router:` block, per design.md §16) → **built-in defaults**.

**A missing config file is not an error.** It yields a fully-defaulted `AutoRouterConfig`, mirroring the absent-versus-corrupt distinction the runtime file draws. `ConfigError` is reserved for present-but-unparseable, unknown keys, and a literal `credential_ref`. No `config.yaml` exists in this repository today and none is created before Wave 2, so a plan that treats absence as fatal fails on its own first verification command.

Ports: `gateway.port` default 8787 (from design.md §16's `http://127.0.0.1:8787`), `gateway.admin_port` default `port + 1`. Ephemeral port 0 is permitted only under `HERMES_AUTO_TEST_EPHEMERAL=1` — Hermes's `model.base_url` is static config and cannot follow an ephemeral port.

**Keys beyond design.md §16.** §16 defines only `gateway.url`, `auto_start`, `startup_timeout_seconds`, `state_dir`. `strict_validation`, `admin_port`, and the whole `upstream.*` block are Phase 2 additions. That is a deliberate design-doc delta to record in the summary, **not** a stop-gate trigger.

## Supervision Contract — frozen in Wave 1, shipped to users' disks

- `<state_dir>/runtime/gateway.json` = `{pid, port, admin_port, instance_id, started_at, exe}`, written atomically (temp + `os.replace`) **after** the port is bound.
- **Stale-PID defeat without `psutil`:** `status`/`doctor` never trust the PID. They `GET /healthz` on the recorded port and compare the returned `instance_id` to the file. Match → running. Connection refused → stale, remove file. Mismatch → another process owns the port, report it. OS-independent, dependency-free, and immune to PID reuse.
- **Stop order:** authenticated `POST /admin/v1/shutdown` first (uvicorn drains in-flight streams) — this is primary *because* Windows has no `SIGTERM`; then `Popen.terminate()`; then `kill()` after a timeout.
- **`restart` = stop + wait for `instance_id` to change.** That is what makes "restart safe mid-session, no stale PID" testable rather than assertable.
- **Spawn:** `start_new_session=True` on POSIX; `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` on Windows, with `CREATE_BREAKAWAY_FROM_JOB` fallback for CI runners that place the parent in a job object. stdout/stderr to a rotating file, **never a pipe** — a full pipe deadlocks an unattended sidecar.
- **Token:** `secrets.token_urlsafe(32)` at `<state_dir>/token`. POSIX `os.open(..., 0o600)`. Windows `icacls <path> /inheritance:r /grant:r "%USERNAME%":F`, with `doctor` reading the effective ACL back.
- **One process, two listeners.** `gateway.main` binds the inference app AND the admin app on the same asyncio loop in one process, and writes `port` and `admin_port` after both binds succeed. `admin_main.py` is a debug-only entry that nothing supervises. This is load-bearing: `stop()` POSTs to `/admin/v1/shutdown` as its primary path on every platform, so an admin listener that no supervised process starts makes graceful drain unreachable and silently degrades every stop to a hard kill.
- **No systemd unit, launchd plist, or Windows Service in Phase 2.** Start-on-demand from the control plugin covers CLI, TUI, and cron. Persistent service mode is `design.md` §11.3 layer 2 and belongs to Phase 11.

## Plan Structure

| Plan | Wave | Delivers |
|---|---|---|
| 02-01 | 1 | Config, state paths, runtime-file contract, token minting with cross-platform permissions |
| 02-02 | 1 | Error-envelope schema, `asyncio_mode` pytest config, redaction sink with per-install salt |
| 02-03 | 1 | Mock upstream server and the byte-exact SSE fixture corpus |
| 02-04 | 2 | Gateway core: ASGI app, routes, ingress, opaque-dict passthrough, SSE relay, upstream client |
| 02-05 | 2 | Provider plugin (R1), the stdlib-only Hermes shim, and the `setup` installer |
| 02-06 | 3 | Admin API on a separate listener and scope, `/healthz`, `/readyz` |
| 02-07 | 4 | Supervisor, CLI subcommands, control plugin, slash commands, lifecycle hooks, `doctor` |
| 02-08 | 5 | Differential proof: byte-identity, tool-call fragmentation fuzz, TTFT perf gate |
| 02-09 | 5 | Live Hermes smoke matrix across CLI, TUI, gateway, desktop, cron |

**Wave count corrected during auto-refine.** Critique found two intra-wave dependencies, which the wave executor treats as circular. 02-06 needs 02-04's graceful drain so it stays in Wave 3; 02-07 therefore moves to Wave 4 and the two test plans to Wave 5. 02-02's dependency on 02-01 was removed rather than reordered, by making the salt directory injectable — 02-02 ships `asyncio_mode` and the three dependencies that Wave 2 needs, so it cannot move later.

**Why 9 plans, not the roadmap's 4:** Phase 2 delivers a complete HTTP service, a plugin in a foreign runtime, cross-platform process supervision, and a differential test harness — four independently verifiable surfaces with different owners. The roadmap's estimate predates the verified discovery that the provider shim is a separate artifact from the sidecar.

## Wave Dependency Graph

```
Wave 1   02-01 (config/runtime/token)   02-02 (schema/pytest/redaction)   02-03 (mock+fixtures)
              │           │                        │                            │
              ▼           ▼                        ▼                            │
Wave 2   02-04 (gateway core) ◄──────────────────────────────────────────────────┘
         02-05 (provider plugin + shim)
              │
              ▼
Wave 3   02-06 (admin + health)      02-07 (supervisor + control plugin)
              │                            │
              ▼                            ▼
Wave 4   02-08 (differential + fuzz + perf)     02-09 (live Hermes smoke)
```

## Phase-Close Gate — run once, after all nine plans

Phase 1's review found that verification scope exceeding file-ownership scope was its single largest defect class, and it has recurred here. **No plan runs `pytest tests/ -q` while a sibling in its wave is still writing test files.** Each plan's gate is scoped to the directories it owns; the whole-tree assertions live here and run once at phase close:

```bash
./.venv/Scripts/python.exe -m pytest tests/ -q
./.venv/Scripts/python.exe -c "from hermes_auto.gateway.schemas import load_schemas, EXPECTED_SCHEMA_IDS; s=set(load_schemas()); assert s==set(EXPECTED_SCHEMA_IDS) and len(s)==8"
./.venv/Scripts/python.exe -m pip wheel --no-deps -w dist-check . && ./.venv/Scripts/python.exe -c "import zipfile,glob; n=zipfile.ZipFile(sorted(glob.glob('dist-check/*.whl'))[-1]).namelist(); assert len([x for x in n if x.endswith('.schema.json')])==8"
test "$(git rev-parse HEAD:design.md)" = "18bb54b36485fa0813ec67f84a74628a9eee3aae"
```

## Proving "Identical Behavior" — the phase's central claim

A differential golden-transcript harness, because a strategy that compares only *assembled* tool-call JSON is precisely the strategy that cannot see fragmentation divergence.

1. **Recorded upstream fixtures**, byte-exact SSE frame sequences covering: streaming text; a single tool call; parallel tool calls; tool-call arguments fragmented mid-JSON — including a fragment splitting a UTF-8 codepoint and one splitting `"argu` / `ments"`; empty deltas; keep-alives; a final usage-only chunk with `choices: []`; a refusal; 401/429/context-length errors; mid-stream disconnect.
2. **Two runs per fixture** — direct against the mock, and through the gateway — asserted on **both** representations: the concatenated frame sequence byte-for-byte after normalizing only `id` and `created` (catches a *dropped* usage chunk), and a semantic reduction that assembles `tool_calls` by `index`, concatenates `function.arguments`, and `json.loads` the result (catches *reframing* corruption).
3. **A chunk-boundary fuzzer** using `hypothesis`, already a dev dependency: re-split the same upstream byte stream at arbitrary offsets and assert both reductions are invariant. **This is the single highest-yield test in the phase** — SSE relays break on frames split across TCP reads, not on well-formed frames.
4. Every emitted chunk validated against `sse-stream-contract.v1` with a hoisted validator, **in CI only**.
5. One live Hermes smoke matrix per surface, asserting only "conversation completes, tools fire" — `design.md` §20.5 says mocks alone are insufficient, but live tests are too slow to be the differential oracle.

## Request Validation Policy

**Trust-and-forward by default; validate structurally and cheaply, always.**

The hot path does: size cap, content-type check, `model`/`messages` key presence, and a **hoisted** validator run against `hermes-auto-metadata.v1` on the `_hermes_auto` envelope only — that object is ours, bounded to about five fields, and costs microseconds.

Full `openai-chat-request.v1` validation is hoisted at startup and runs (a) unconditionally in the CI contract suite and (b) at runtime under `gateway.strict_validation: true`, **default off**.

Rationale against budget: the 100 ms p99 / 1% allowance covers the *entire* overhead including the loopback hop and JSON re-serialization. The ~50 ms figure is `check_schema` and disappears when hoisted, but hoisted validation of a 100–200 KB `messages` array against a recursive schema is still plausibly 3–15 ms. Plan 02-04 includes a measurement; **if hoisted cost lands under 2 ms, flip the default to on.** Measurement decides, not taste.

## Codebase Map

`.planning/CODEBASE.md` does not exist. Phase 1 delivered 36 Python files and 7 schemas, so `/legion:map` would now produce useful retrieval context. Not blocking — the phase's read targets are enumerated per plan — but worth running before Phase 3, when the code is large enough that plan authors cannot hold it all.
