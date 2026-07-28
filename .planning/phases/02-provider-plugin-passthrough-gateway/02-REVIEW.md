# Phase 2: Provider Plugin & Passthrough Gateway — Review Summary

## Result: PASSED

**Cycles used**: 2 of 3
**Panel**: testing-qa-verification-specialist, engineering-security-engineer, testing-api-tester,
engineering-infrastructure-devops (dynamic, non-overlapping rubrics)
**Completed**: 2026-07-28
**Tests**: 882 → **971 passing, 4 skipped, 0 failing**

## Findings summary

| | Found | Resolved |
|---|---|---|
| BLOCKER | 9 | 9 |
| WARNING | 20 | 17 (3 deferred with reasons) |
| SUGGESTION | 12 | 6 |

All four reviewers returned NEEDS WORK in cycle 1. All blockers closed in cycle 1; the security
findings — which had to be produced without a shell — were adjudicated empirically in cycle 2.

## Cycle 1 — nine blockers, five of them live defects

| Finding | Reviewer | Resolution |
|---|---|---|
| Lone UTF-16 surrogate in a request → gateway 500, direct 200 | API | `encode_body` uses `surrogatepass` |
| gzip upstream on the non-streaming path → `RemoteProtocolError` | API | `complete()` drains `aiter_raw()`; both verbs return wire bytes under one header policy |
| 400 envelope body echoed `root_session_id` to the caller | API + Security | reports `exc.validator` and the path, matching `validate_request` |
| A failing gateway deleted a **healthy** gateway's runtime file | Infra | `published` flag + identity-scoped clearing |
| An IPv6-bound gateway declared stale and orphaned | Infra | probe host resolves from config; every `getaddrinfo` result must bind |
| A bearer token and raw prompt could be logged, suite green | QA | log scan widened to 12 modules, keys **and** credential-valued expressions |
| `aiter_raw` → `aiter_bytes` passed all 882 tests | QA + API | AST guard moved to `upstream.py`; `content`/`text` added |
| `BANNED_KEYS` parametrized over `BANNED_KEYS` | QA | pinned against a literal reproduced in the test file |
| Timing-oracle guard counted `ast.Call` nodes, not reachability | QA | spy requiring exactly one comparison on every denial path |

**The orphan incident was root-caused.** Two sidecars ran three hours on the developer's machine
with no `runtime.json`, unreapable by `hermes auto stop`. `serve()`'s `finally` called
`clear_runtime()` unconditionally, so a gateway that failed to bind deleted a healthy sibling's file.
The inference bind sits *outside* the `try` — the guard existed and was one line too high.

**`.venv/Scripts/python.exe` is a trampoline**: every gateway is two PIDs, and killing only the
trampoline leaves the real interpreter listening. 40 pre-existing orphans (20 gateways) were reaped,
several still holding ports.

## Cycle 2 — nine security findings confirmed, one refuted

The cycle-1 security reviewer ran without a shell and marked every finding static-derived rather
than dressing reading up as testing. Cycle 2 re-ran them with one.

**Confirmed and fixed**: a 502 body returned `upstream at https://apiuser:sup3rs3cret@…` verbatim,
and the log carried it identically since `upstream_base_url` is not a `BANNED_KEYS` entry; the salt
bypassed `secure_write` and had no permission readback, despite `auth.py` claiming a single
implementation precisely so three copies could not drift — it had already drifted; the Windows ACL
allowlist compared only the leaf after the final backslash, so `CORP\dasbl` and `OTHERBOX\dasbl` both
passed as the current account; `health/probe.py` returned `str(exc)`, embedding the whole `base_url`
on an unauthenticated endpoint; `render()`'s sequential replace produced a real `SyntaxError` in a
module Hermes imports at startup; `commands.py` reintroduced the IPv4 hardcode for `doctor`.

**Measured, not asserted**: the `secure_write` Windows residual was understated by four orders of
magnitude — `open+write+flush+fsync` median **16 ms**, full `secure_write` median **188 ms**, against
a docstring saying "microseconds."

**Refuted**: CPython *does* honour `mode=0o700` in `os.mkdir` on Windows and it blocks inheritance,
measured against a control (`mode=0o700` → SYSTEM/Administrators/OWNER RIGHTS; default →
`Authenticated Users:(I)(M)`). A narrower residual survives — a *pre-existing* directory is never
re-narrowed — and that is now **reported** rather than silently overridden, because not re-narrowing
an operator's directory is a deliberate choice and leaving it unreported was the actual defect.

## The governing finding

**Verification quality, one level up from where Phase 2 execution found it.**

During execution, ~45 of ~135 `> verification:` commands were found vacuous. The agents wrote
replacements. **Those replacements were self-graded, and the review found the same defect in them.**
Of 18 source mutations, 13 went red and 5 went green — and all five shared one signature:

> the test is written against the artifact it is meant to constrain

`BANNED_KEYS` parametrized over `BANNED_KEYS`. The log scan scoped to the one module whose author
wrote it. `compare_token` **counted** rather than **reached**. `aiter_raw` forbidden in `relay.py`,
the file that never called it.

All five now go red under the same mutation, independently re-verified after the fixes landed.

## Orchestrator error, recorded

Commit `fd77adc` landed all of cycle 1's tests but only two of its source fixes, leaving 13 tests
failing against code that no longer contained their fix. Cause: the mutation-verification loop ended
each mutation with `git checkout -- <file>`, which reverts to **HEAD**, not to the uncommitted
working state — silently destroying the fix agents' in-flight edits to `ingress.py`, `upstream.py`
and `redaction.py`. The mutation *evidence* was valid; the cleanup destroyed what it had verified.

Found by the cycle-2 agent, not by the orchestrator. All four fixes re-derived from the fix agents'
reports and re-verified as load-bearing (2, 2 and 3 failures respectively under mutation).

**Rule for later phases: commit before mutating.**

## Success criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Identical behaviour, gateway vs direct | **Met** |
| 2 | Streaming, tool calls, usage, errors match | **Met** — byte-identity on 12 fixtures against a real detached sidecar; harness proven able to go red by 16 injected corruptions |
| 3 | `start\|stop\|status\|doctor` on Win/macOS/Linux | **Met on Windows; not provable here for POSIX.** Paths read as credible; CI matrix covers ubuntu/macos for non-spawning suites |
| 4 | Bearer auth, loopback only, no CORS | **Met** — "no CORS" asserted for the first time, with a control test proving the negatives can fail |
| 5 | CLI, TUI, gateway, desktop, cron smoke tests | **NOT MET — 3 of 5.** Amendment recommended; see below |
| 6 | Restart safe mid-session, no stale PID | **Met after fixes.** Was not met at review time — two live unreapable sidecars reproduced |
| 7 | No raw prompt in any log | **Met, and now non-vacuously.** The sink was inert (`get_logger` never called `setLevel`, so every `.info()` was discarded before reaching the formatter). That, not restraint, is why the log was 248 bytes |

### Criterion 5 — recommended amendment

> CLI, TUI, and cron smoke tests pass; the messaging gateway and desktop are covered by a
> credentialed job and a Node e2e job respectively.

Endorsed by two reviewers independently. Both gaps are **resourcing, not capability**: every
messaging platform gates on a third-party credential, and desktop is Electron — though its Python
backend *is* the `tui_gateway` dispatcher the TUI test drives, so only the renderer is uncovered
(verified in the Hermes checkout at `apps/desktop/e2e/real-session-builder.ts:74`).

**The cron gap is different in kind** and the amendment should commit to closing it rather than
absorbing it: it is a fixture-corpus hole the project can close itself.

## Deferred, with reasons

- **The SSE corpus is streaming-only.** The recorded gap was *mis-sized* — the non-streaming path is
  **25% covered** (3 error fixtures run through `complete()`), not 0%. Uncovered: a 200
  `chat.completion`, tool calls in a non-streaming message, a chunked 200, a compressed 200. This
  matters more than it sounds: `conversation_loop.py:1949` sets `agent._disable_streaming` after
  **one** stream failure, switching the rest of the session permanently onto that branch.
- **`/readyz` remains unauthenticated and discloses the upstream host and port.** The credential was
  removed; the host disclosure is a design choice (a monitor must reach it) and was documented rather
  than changed unilaterally.
- **The TTFT gate skips under `CI`**, so design.md §15.4's latency budget is enforced by no automated
  gate. Needs a nightly job on a dedicated runner with `CI` unset.
- **The e2e job has no floor on what must run** — an all-skip result exits 0. `hermes-compat.yml`'s
  `report` job gets this right and is the model to copy.
- **`RedactingFormatter` handles `record.msg`/`args` but not `extra=`.** Closed in cycle 2 by
  substitution rather than removal (a format string naming a deleted attribute raises inside the
  formatter, which `logging` swallows, costing the whole record).

## Operational note

Disk reached **0 bytes free** mid-run, aborting a suite with `OSError: Errno 28`. 12 GB freed from
`pytest-of-dasbl`. `tmp_path` retention plus per-test gateway logs accumulate faster than the
three-run cap implies — worth a fixture-level cleanup before Phase 3 adds more integration tests.
