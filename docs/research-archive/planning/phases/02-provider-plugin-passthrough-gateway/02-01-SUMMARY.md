# Plan 02-01 Summary — Config, State Paths, Runtime File, Token Auth

**Status**: Complete with Warnings
**Wave**: 1 · **Agent**: engineering-backend-architect (+ security-engineer, project-manager-senior concerns)
**Requirements**: R4
**Verification**: 25 run / 24 passed, plus 10 stronger checks the agent added

## Files created (8, exactly the declared set)

`src/hermes_auto/config.py`, `src/hermes_auto/state/paths.py`, `src/hermes_auto/state/runtime.py`,
`src/hermes_auto/gateway/auth.py`, and their four unit test modules.

## Contracts frozen here (read by 02-04 and 02-07)

**State directory (this platform)**: `C:\Users\<user>\.hermes\auto-router`

**Runtime file** — `json.dumps(..., sort_keys=True, indent=2)` + trailing newline, byte-deterministic:

```json
{ "admin_port": 8788, "exe": "...python.exe", "instance_id": "<32 hex>",
  "pid": 4321, "port": 8787, "started_at": "2026-07-26T12:00:00+00:00" }
```

**Token**: `<state_dir>/token`, `secrets.token_urlsafe(32)` (43 chars). Windows path is
`os.open(O_CREAT|O_EXCL|O_WRONLY, 0o600)` then `icacls /inheritance:r /grant:r`.
`icacls` was available; readback confirmed a single non-inherited ACE.

## The one failed verification is a guard defect, not a work defect

PV6 forbids `pyproject.toml`, `gateway/schemas.py`, `telemetry/`, `data/` — but three of those are
plan **02-02's** legitimate same-wave deliverables. The agent verified none were its own, refused to
revert a sibling's work, and reported it. Scoped to `telemetry/` alone the guard is clean.

This is the same defect class that cost a cycle in Phase 1 (plan 01-02's guard naming 01-01's files).
It recurred because the Phase 2 plans were authored with whole-tree guards rather than
sibling-aware ones. Corrected in 02-04 … 02-09 before Wave 2 dispatch.

## Decisions a reviewer should know

1. **The plan contradicted itself on missing config.** Task 1's test list said a missing file raises
   `ConfigError`; the same task's prose and `02-CONTEXT.md` said it returns defaults. Resolved so both
   hold: the *implicit* chain absent → full defaults; an *explicitly named* path (`load_config(p)` or
   `HERMES_AUTO_CONFIG`) missing → raises, because naming a path asserts it exists. Keeps 02-04's
   first gate working while a typo'd path still fails loudly.
2. **`admin_port` added to `RuntimeFile`.** The plan listed five fields; `02-CONTEXT.md`'s supervision
   contract specifies six and makes `admin_port` load-bearing — `stop()` POSTs the admin listener as
   its primary path on every platform, since Windows has no `SIGTERM`. Omitting it would have forced
   an on-disk migration in the very plan meant to freeze the contract.
3. **Unknown-key rejection is scoped to blocks this phase owns** — strict inside `gateway:`/`upstream:`
   so `strict_validaton` errors rather than silently defaulting; permissive for `routing:`,
   `constraints:`, `candidates:`, `telemetry:`, `learning:`, which later phases own.
4. **The Windows ACL check is an allowlist, not the plan's denylist.** The real machine ACL also
   granted `CodexSandboxUsers`, which no denylist would have named. Every ACE must now resolve to the
   current account, plus an inherited-flag check.
5. **`gateway.port` derives from `gateway.url`** unless set explicitly, so a config changing only the
   URL cannot bind one port while Hermes dials another.
6. **`mint_token()` does not raise when the permission readback fails.** A gateway refusing to start
   on a machine without `icacls` pushes the user to run it another way — strictly less safe.
   `token_permissions_ok()` is the authority; `doctor` reports it.

## Vacuous verifications found and strengthened

Eight of 19 were substring greps satisfiable by a docstring. Ran as written *and* replaced:

| Weak | Why it passed wrongly | Replacement |
|---|---|---|
| `'env:' in getsource(config)` | the docstring contains `env:` | 4 literal secrets → all raise `ConfigError` |
| `grep -q 'os.replace'` / `'fsync'` | a comment satisfies both | AST: `write_runtime` *calls* both, never `open()`s the target |
| `'compare_digest' in src` | docstring satisfies it | AST: `compare_token`'s body calls it, no `==` on token names |
| `'os.kill' not in src` | a rephrased probe evades it | AST: no `signal`/`subprocess`/`psutil`/`ctypes` import |
| `sys.modules` third-party check | passes if imported lazily | AST source-level: zero third-party in `paths`/`runtime`; only `yaml` in `config` |

The agent's own first draft of `test_module_contains_no_liveness_probing` had this bug — it fired on
the docstring documenting the exclusion. Rewritten AST-based rather than deleted.

## Issues raised (real, out of scope here)

- **Residual Windows exposure in `secure_write`**: between `os.open` and `icacls` the file briefly
  exists with inherited ACLs. Closing it needs a security descriptor at creation time (`pywin32`), a
  dependency this phase refuses. Documented in the docstring; *not* claimed closed.
  `permissions_ok` proves only the end state.
- **No directory `fsync` after `os.replace`** in `write_runtime`. Atomic, but on POSIX not durable
  across power loss. Outside the plan's three stated properties.
- Task 3's verification mints a **real** token into the developer's state directory. That is the
  plan's intent (it proves platform behavior), but it is a real artifact, not a fixture.
- `design.md` §16 lacks `gateway.strict_validation`, `gateway.port`, `gateway.admin_port`, and the
  whole `upstream.*` block. `02-CONTEXT.md` pre-authorizes these deltas.
