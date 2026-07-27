# Plan 02-07 Summary — Supervisor, CLI, Control Plugin (+ authorized ingress fix)

**Status**: Complete
**Wave**: 4 · **Agent**: engineering-infrastructure-devops (+ senior-developer concerns)
**Requirements**: R2, R4
**Verification**: 23 run / 23 passed
**Tests**: 693 → 761 passing (+68, none lost)

## Files

`src/hermes_auto/{supervisor,commands,plugin,cli}.py` (879 / 869 / 209 / 127 lines),
`pyproject.toml` (**only** `[project.scripts]` and `[project.entry-points."hermes_agent.plugins"]` —
no dependency edits), `tests/integration/test_supervisor.py` (20 tests),
`tests/unit/test_cli.py` (39 tests).

**Authorized scope extension**: `src/hermes_auto/gateway/ingress.py` (modified),
`tests/unit/test_ingress_auth.py` (new, 9 tests).

## The authorized ingress fix — red before, green after

Plan 02-06 found `ingress.py:195` failing open: `compare_digest(b"", b"")` is `True`, so an app with no
token admitted a request with no header — the opposite of what the comment directly above it stated.

Bypass confirmed empirically first (`compare_token("", "")` → request **admitted**), then tests written
and run against the unfixed code: **7 failed, 2 passed**. Fix applied — explicit absent-token branch
plus a `_DECOY_TOKEN` decoy comparison, mirroring `admin.py` — then **9 passed**. The decoy is asserted
random and non-literal, and a structural test requires ≥2 `compare_token` calls so a well-meaning
"early return" refactor cannot silently reintroduce a timing oracle.

## Two real bugs found by measuring instead of asserting

**1. The stop polling prevented the shutdown it was waiting for.** `stop()` took **13.4 s** against a
gateway that exits in **0.41 s**. uvicorn's graceful drain ends with `while server_state.connections`,
so each timed-out `/healthz` poll left a half-finished connection that blocked the drain — every poll
feeding the next. Replaced HTTP polling with the runtime file (which the exiting process clears itself)
plus a bind test.

| | before | after |
|---|---|---|
| `stop` | 13.4 s | **0.12 s** median |
| `restart` | — | 1.17 s |
| `start` | — | 1.05 s |

n=5; `instance_id` changed every cycle.

**2. An environment fact made a required behaviour unreachable.** On this machine a TCP connect to a
**never-used** loopback port *times out* rather than being refused — a local security product drops the
SYN. That makes the supervision contract's "connection refused → stale, remove the file" branch **dead
code**, so no stale runtime file would ever be cleaned. Fixed by deciding staleness with `bind()` — a
local kernel question no firewall distorts. Verified: a stale file is now detected and removed; a held
port reports `unresponsive` and is left alone.

## Vacuous verifications: 7 of 23

Built a deliberately wrong module that evades greps via `import_module("ps"+"util")` and
`getattr(subprocess, "PI"+"PE")` — **all five** of Task 1's greps passed against it.

Two checks also failed in the **opposite** direction: `'psutil' not in src` and
`'reroute'/'feedback' not in s` fired on the module's own docstrings *documenting the exclusion* — the
same inversion plan 02-06 hit. The docs were reworded to pass as written, and AST/behavioural
replacements added: import-graph proof, `Popen` kwarg proof, a breakaway-is-a-retry ordering proof, and
`set(ctx.slash) == {"auto"}`.

## Decisions a reviewer should know

1. **`Status` gained `kind`** (defaulted, additive). Callers must distinguish "nothing running" from
   "someone else owns your port" to choose an exit code; string-matching `detail` is exactly the fragile
   coupling to avoid.
2. **`doctor` now mints the admin token when absent** — 02-06's request, initially under-delivered by
   wiring it only into `setup`. Safe because `admin.py` reads it per request. It deliberately **never**
   re-mints the *inference* token: `app.py`'s lifespan reads that once at startup, so rotating it would
   lock out a running gateway.
3. **Killing by PID happens only after re-confirming `instance_id` immediately beforehand.** This
   narrows the reuse race to one loopback round trip — stated as narrowed, not closed.
4. **`enable_control_plugin` does surgical text edits, never a YAML re-dump** (which would destroy the
   user's comments), with a backup, atomic replace, and a re-parse that must equal the original plus
   exactly one name — else it restores and prints `hermes plugins enable hermes-auto-control`. Refuses
   inline/flow lists outright.

## Constraint verification

**Zero Hermes modifications confirmed**: checkout `git status --porcelain` empty; the real `config.yaml`,
`plugins/` directory, and state dir were never written — every manual run used a temp `--hermes-home`
and `HERMES_AUTO_STATE_DIR`.

Wheel gate re-verified after the `[project.scripts]` addition: 8 schemas, all four new modules, both
entry points. `pip install -e . --no-deps` re-run only to materialise the console script.

## Issues raised (real, out of scope here)

1. **The SYN-drop behaviour is machine-wide** and will affect any future code treating "connection
   refused" as a liveness signal — **including plan 02-09's live smoke matrix.**
2. **`HERMES_AUTO_STATE_DIR` outranks config**, so *no* in-process test can use a second state dir. The
   agent's first attempt at a foreign-gateway test silently wrote into the first install's directory.
   Relevant to 02-08 and 02-09.
3. **A bare TCP listener on the recorded port reports `unresponsive`, not `foreign`** — `/healthz` times
   out rather than answering, and only an HTTP responder yields `foreign`. Honest, but the two states
   are less distinguishable than the supervision contract implies.
4. **`design.md` §4.2's six other `/auto` commands are deliberately unregistered**; the phases that
   deliver them are named in `admin.py`'s 501 bodies. `hermes auto explain --last` is likewise not
   delivered in Phase 2.
