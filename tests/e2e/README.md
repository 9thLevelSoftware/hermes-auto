# Live Hermes surface tests

## A skip is not a pass

This is the only sentence in this file that matters. When you read
`5 passed, 6 skipped` in CI output, **six things were not tested**. Nothing in a
skip count tells you whether the product works; it tells you the suite was not
configured to find out. Every skip here therefore carries a reason that names
the specific missing piece and what would make it runnable, and CI runs with
`-rs` so the reasons appear in the log and in the job summary rather than
collapsing into a number.

The CI job on this repository is **expected to skip today**. It has no Hermes
installation, so it emits `hermes-agent not installed: …` and exercises no
surface. That is honest and useful — it makes the absence visible on every
build. It is not evidence that anything works.

## What the suite needs

| Requirement | How it is satisfied | Skip reason when absent |
|---|---|---|
| Opt-in | `HERMES_AUTO_E2E=1` | `set HERMES_AUTO_E2E=1 to run live surface tests` |
| A real Hermes install | a source checkout whose virtualenv has `hermes-agent` installed | `hermes-agent not installed: <paths looked at>` |
| A reachable upstream | the in-repo scripted upstream by default, or `HERMES_AUTO_E2E_UPSTREAM` | `upstream unreachable at <url>` |
| A drivable surface | see the surface table below | a surface-specific reason |

### Environment variables

| Variable | Meaning |
|---|---|
| `HERMES_AUTO_E2E` | Set to `1` to opt in. Nothing runs otherwise. |
| `HERMES_AUTO_E2E_HERMES_ROOT` | Hermes source checkout to drive. Defaults to `$HERMES_HOME/hermes-agent`. |
| `HERMES_AUTO_E2E_UPSTREAM` | A real OpenAI-compatible base URL. When set, replaces the scripted upstream and is probed for reachability. |
| `HERMES_AUTO_E2E_UPSTREAM_CREDENTIAL_REF` | `env:NAME` — the *name* of the variable holding the upstream credential. Never a credential value. |
| `HERMES_AUTO_E2E_UPSTREAM_MODEL` | Model forwarded upstream. Only meaningful with a real upstream. |
| `HERMES_AUTO_E2E_TIMEOUT` | Per-surface budget in seconds. Default 240. |

### Running it locally

```bash
HERMES_AUTO_E2E=1 ./.venv/Scripts/python.exe -m pytest tests/e2e -q -rs
```

Against a real endpoint instead of the scripted corpus:

```bash
HERMES_AUTO_E2E=1 \
HERMES_AUTO_E2E_UPSTREAM=https://api.example.com/v1 \
HERMES_AUTO_E2E_UPSTREAM_CREDENTIAL_REF=env:MY_PROVIDER_KEY \
HERMES_AUTO_E2E_UPSTREAM_MODEL=gpt-4o-mini \
./.venv/Scripts/python.exe -m pytest tests/e2e -q -rs
```

The suite never writes to your real `$HERMES_HOME` or your real state directory.
It creates a temporary `HERMES_HOME`, installs the provider shim there, sets
`HERMES_AUTO_STATE_DIR` explicitly — because that variable *outranks* the config
file, and a fixture that only wrote `state_dir` into YAML would silently drive
your real install — and removes both in a `finally` that runs even when a test
fails.

## Which surfaces are exercised

`design.md` §20.5 names five: CLI, TUI, messaging gateway, desktop, cron.

| Surface | Status | Driven how, or why not |
|---|---|---|
| **CLI** | exercised | `python -m hermes_cli.main -z "<prompt>" --cli` |
| **TUI** | exercised | `python -m tui_gateway.entry`, line-delimited JSON-RPC on stdin — the protocol the Ink front end speaks. No TTY needed. |
| **cron** | partly exercised | `hermes cron create` + `hermes cron run <id>`. Reaches the provider; completion is not provable on the scripted corpus (see below). |
| **Messaging gateway** | **skipped** | Every platform in `gateway/platform_registry.py` declares a `required_env` naming a third-party credential (Telegram bot token, Discord token, …). Without one Hermes prints `No platforms configured` and starts no listener. |
| **Desktop** | **skipped** | `apps/desktop` is an Electron app with no Python driver — `npm install`, a package build, and a display server. Its own harness is `apps/desktop/playwright.config.ts`, a Node suite. |

Two honest qualifications on that table:

* **The desktop's Python backend is not untested; its renderer is.** The desktop
  talks to the same `tui_gateway` dispatcher the TUI test drives, over a
  WebSocket instead of stdio. So the Hermes-side path is covered and the
  Electron UI is not. That is a weaker claim than "desktop passes" and it is the
  true one.
* **The cron surface calls with streaming disabled.** Its recorded request body
  carries no `stream` key, while every fixture in `tests/fixtures/sse/` is a
  `text/event-stream` body. The OpenAI SDK cannot parse an SSE body as a
  completion object and fails with `vars() argument must have __dict__
  attribute`. The request still reaches the upstream through the gateway
  unmodified, which is what the cron test asserts; the completion and tool-call
  assertions skip with that reason. Point `HERMES_AUTO_E2E_UPSTREAM` at a real
  endpoint and they run.

## What each test claims, and what it does not

Per drivable surface, four separate tests so a failure names the broken
property:

1. **reaches the provider** — a `chat/completions` request arrived upstream with
   `model: auto:balanced`, and the private `_hermes_auto` envelope was stripped
   by the gateway before forwarding.
2. **completes a conversation** — the assistant's reply reached the surface.
3. **fires a tool call and returns its result** — asserted on the *next* upstream
   request carrying a `role: "tool"` message with the fixture's `tool_call_id`.
   That proves the SDK reassembled a streamed `tool_calls` delta that the
   gateway relayed. The fixture names a tool Hermes does not have, so the result
   itself is a "tool does not exist" message — the *dispatch and return* is what
   is proven, not the tool's own behaviour.
4. **leaves no raw prompt in the sidecar log** — a canary string unique to this
   suite is absent from the log, and no JSON log record contains a banned key.

This suite is deliberately thin. It does **not** compare bytes between the
gateway and a direct call, because live subprocesses are far too slow and too
coarse to be that oracle. Plan 02-08 owns byte-level identity, chunk-boundary
fuzzing, and the TTFT gate. `design.md` §20.5 requires real-path coverage
because "mocks alone are insufficient" for resolution chains, configuration, and
security boundaries — the resolution chain here (directory-scan discovery of a
shim in `$HERMES_HOME`, a foreign interpreter, a bearer token, a loopback hop)
is exactly the kind mocks cannot stand in for, and it is what these tests cover.

## Restart safety (ROADMAP criterion 6)

One further test does not belong to any single surface:
`test_sidecar_restart_mid_session_is_safe_and_leaves_no_stale_pid`. It holds the
upstream's first byte for 20 s, waits until the upstream has *actually recorded*
the request — so the sidecar is demonstrably mid-relay, not idle — then restarts
it and asserts the `instance_id` changed, that the runtime file names the process
now answering rather than the old PID, and that a fresh turn completes.

It deliberately does **not** assert that the in-flight request survives. It does
not, and it should not: a graceful drain finishes streams already flowing, and
this one is still waiting on its upstream's first byte. "Safe" means the
supervision state stays consistent and the next request works — not that a
restart is invisible to a caller mid-stream.

## Known weakness in the log assertion

The sidecar currently writes almost nothing to its log: uvicorn is configured
with `access_log=False`, and a completed conversation leaves only the startup
lines behind. The leak assertion is therefore real but has very little to bite
on — it would not catch a leak in a code path that logs nothing today and starts
logging tomorrow. Treat it as a tripwire, not as proof of the redaction
boundary; `tests/unit` and `tests/contract` test the redaction sink directly.
