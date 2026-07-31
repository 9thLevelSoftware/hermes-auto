# Plan 02-09 Summary — Live Hermes Surface Smoke Matrix & CI

**Status**: Complete with Warnings
**Wave**: 5 · **Agent**: testing-qa-verification-specialist (+ infrastructure-devops concerns)
**Requirements**: R1, R2, R3
**Verification**: 22 run / 22 passed
**Tests**: 882 passing, 4 skipped. `HERMES_AUTO_E2E=1 pytest tests/e2e -q` → 12 passed, 4 skipped in 50 s

## Files

`tests/e2e/{conftest.py,test_hermes_surfaces.py,README.md}` (new),
`.github/workflows/ci.yml` (modified — **61 insertions, 0 deletions**; the three Phase 1 jobs untouched).

## My coverage premise was half wrong — this changes the answer

I briefed this plan that Hermes is "a source checkout, not an installed distribution," and that a
dedicated e2e venv would be needed. **Both were wrong.**

- **Hermes is installed.** `C:\Users\<user>\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`
  reports `hermes-agent 0.19.0`, and `import providers` succeeds there **from any cwd** — it is an
  installed package, not a cwd-relative import. Real Hermes surfaces are drivable today.
  **No dedicated e2e venv is needed.**
- **The TUI is drivable headlessly.** `tui_gateway/entry.py` is a line-delimited JSON-RPC server over
  stdio that emits `gateway.ready` and accepts `session.create` / `prompt.submit`. The Ink front end is
  a *client* of that protocol. No TTY, no curses.
- **The desktop assessment was right.** Electron, no Python driver.

## Surfaces exercised: 3 of 5

| Surface | Result |
|---|---|
| **CLI** | **Exercised.** `hermes_cli.main -z … --cli`; stdout was exactly `Hello, synthetic world.` |
| **TUI** | **Exercised.** JSON-RPC over stdio; `session.info` reports `provider: "hermes-auto"`, `message.complete` carries the reply |
| **cron** | **Partly.** `cron create` + `cron run` reaches the provider (endpoint = our gateway, `model: auto:balanced`). Completion/tool assertions **skip** — see finding 1 |
| **Messaging gateway** | **Skipped.** Every platform in `gateway/platform_registry.py` declares a `required_env` naming a third-party credential. Hermes prints `No platforms configured` and starts no listener. A credential decision, not a code one |
| **Desktop** | **Skipped.** Electron; needs `npm install` + package build + display. Its harness is `apps/desktop/playwright.config.ts`, a Node suite. Its Python backend **is** the `tui_gateway` dispatcher the TUI test drives — so the backend is covered and the renderer is not |

**ROADMAP criterion 5 is NOT met as written.** Recommended amendment: *"CLI, TUI, and cron smoke tests
pass; messaging gateway and desktop are covered by a credentialed job and a Node e2e job respectively."*
Both gaps are resourcing, not capability.

## Criterion 6 (restart safe mid-session, no stale PID) — now proven

A test holds the upstream's first byte for 20 s, waits until the upstream has **recorded** the request
(so the sidecar is demonstrably mid-relay, not idle), then restarts and asserts `instance_id` changed,
the runtime file names the new PID, and a fresh turn completes. It deliberately does **not** claim the
in-flight request survives — it does not, and should not.

## Criterion 7 (no raw prompt in logs) — passes, but weakly

See finding 2: the sidecar log is nearly empty, so the criterion passes with almost nothing to bite on.

## Findings

1. **The SSE fixture corpus is streaming-only, and Hermes has non-streaming paths.** Cron's recorded
   body has no `stream` key; the SDK then fails with `vars() argument must have __dict__ attribute`.
   The TUI's title-generation and auxiliary calls are also non-streaming. **This also limits 02-08** —
   the differential proof cannot cover the non-streaming path with the current corpus. A non-streaming
   `application/json` fixture belongs to 02-03's corpus.
2. **The sidecar log is nearly empty.** `main.py` sets `access_log=False`, and a completed conversation
   leaves only 248 bytes of uvicorn startup lines. Operationally there is **no request trace at all**
   for diagnosis.
3. **02-08's TTFT p99 gate is load-sensitive** — 0.40 s under concurrent load, passing alone. It will
   flake on a shared CI runner.
4. **Plan defect (deviated, with evidence)**: the plan's Hermes gate,
   `importlib.metadata.version("hermes-agent")` evaluated in *this* venv, can never succeed — it would
   have produced a permanently-skipping suite wearing a green tick. The Hermes interpreter is queried
   by subprocess instead.
5. **Vacuous verifications: 5 of 14, demonstrated by construction.**
   `'yield' in s and ('finally' in s or … or 'yield' in s)` reduces to `'yield' in s`; the five-surface
   `s.index()` check and the `'log' … 'prompt'` check pass on a pure-docstring file; the
   three-skip-reasons grep passes on a docstring that merely lists them; and
   `'HERMES_AUTO_E2E' not in s or 'env' not in s` **passes on a job that does force the suite to run.**
   Fourteen AST/behavioural replacements written and passing.
6. **Its own log test was vacuous until fixed.** A shim-removal mutation (7 tests failed, proving the
   suite depends on real registration) showed 3 tests still *passing* with zero traffic — the leak
   tests. A positive control was added asserting the canary reached the upstream first.
7. **Self-inflicted CI defect, caught and fixed**: a `pytest … | tee` step would have reported green on
   a failing suite without `set -o pipefail` — the exact failure mode the job exists to prevent.

## Constraints verified

Hermes checkout `git status --porcelain` empty; real `$HERMES_HOME` has no `plugins/` directory and no
`auto_router` block; **0 orphan sidecars**; `design.md` blob pin intact; `CREATE_NO_WINDOW` on all 6
spawns this plan owns (the constant was initially declared in `conftest.py` without being applied to
its own two spawns — caught by an AST sweep and fixed).
