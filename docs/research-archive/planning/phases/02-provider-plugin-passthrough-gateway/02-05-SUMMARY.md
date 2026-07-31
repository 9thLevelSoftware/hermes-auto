# Plan 02-05 Summary — Provider Plugin & Hermes Shim Installer

**Status**: Complete with Warnings
**Wave**: 2 · **Agent**: engineering-senior-developer (+ testing-api-tester concerns)
**Requirements**: R1
**Verification**: 23 run / 20 passed — **all 3 failures are plan defects**, plus 5 stronger replacements
**Tests**: 622 passing overall (76 from this plan)

## Files (6, exactly the declared set)

`src/hermes_auto/provider.py`, `src/hermes_auto/hermes_shim/{__init__,template,installer}.py`,
`tests/unit/test_provider_profile.py` (32 tests),
`tests/integration/test_shim_installer.py` (44 tests).

## The finding that sharpens `02-CONTEXT.md` § VERIFIED HERMES FACTS item 2

The agent ran `design.md` §5.1's form against real Hermes discovery. **`02-CONTEXT.md`'s correction was
too generous.** It says following §5.1 "does not register anything" — true of the literal snippet, but
it misses the more dangerous adjacent mistake:

- §5.1 as literally printed contains no `register_provider` call → nothing registers.
- **Charitably add `register_provider(HermesAutoProfile)` — passing the class — and it registers
  successfully.** `get_provider_profile("hermes-auto")` returns it, `.name` reads `hermes-auto`, doctor
  and the model picker both look healthy. Then every method is unbound:
  ```
  type: <class 'type'>
  build_extra_body: TypeError: ProviderProfile.build_extra_body() missing 1 required positional argument: 'self'
  ```
  **This is worse than "unknown provider"** — it passes setup and fails at first inference.
- The obvious fix also fails: `Bad()` → `TypeError: ProviderProfile.__init__() missing 1 required
  positional argument: 'name'`, because bare class attributes populate no dataclass field.

Both failure modes are now pinned as tests. The registration test is also proven non-vacuous:
`test_real_hermes_does_not_know_the_provider_before_installation` confirms `hermes-auto` does not
resolve from an empty `HERMES_HOME`, so the positive test cannot be passing on a pre-existing install.

## The 3 failed verifications are plan defects

**1 & 2 — `build_profile()` cannot run in the sidecar venv** (PV3 and Task 1 verification #3):

```
ModuleNotFoundError: No module named 'providers'
```

The plan's own subprocess form fails too, for a second and more interesting reason. Under Hermes's
bundled interpreter with `sys.path.insert(src)`:

```
File "src\hermes_auto\config.py", line 50: import yaml
ModuleNotFoundError: No module named 'yaml'
```

**`hermes_auto` is not importable in the Hermes environment** — the isolation constraint working as
designed, and precisely why the shim duplicates the envelope rather than importing it. Neither
interpreter alone can run the check.

**The working form** is the *sidecar* interpreter with `cwd` at the Hermes checkout: `providers/base.py`
and `providers/__init__.py` are stdlib-only, so `providers` resolves from cwd while `yaml`/`jsonschema`
resolve from the venv. Verified end-to-end, including a no-arg envelope from the real `ProviderProfile`
that validates. Unit tests use the in-process equivalent, so `build_profile` is tested against the
**real dataclass**, never a stub.

**3 — `assert 'hermes_auto' not in SHIM_SOURCE` can never pass.** The envelope key is literally
`_hermes_auto`; the substring occurs exactly once and only as the protocol key. The check's own message
says "shim must not import this project" — an AST question. The replacement proves it
(`shim imports ['providers', 'uuid']`). The agent did **not** obfuscate the key to satisfy the grep.

## Vacuous verifications found and replaced

| Weak check | Why vacuous | Replacement |
|---|---|---|
| `'register_provider' not in s or 'def ' in s` | tautology — any Python file contains `def ` | AST: `register_provider` in no `Call` node in `provider.py` |
| `grep -q 'MARKER'` / `'os.replace'` | a comment or docstring satisfies both | AST: `install` **calls** `os.replace` and `os.fsync`; `MARKER` appears in a `startswith` ownership check |
| `'register_provider(' in SHIM_SOURCE` | satisfied by a docstring **and by `register_provider(HermesAutoProfile)` — the class bug above** | AST: the call's arg must be a module-level name bound to a constructor `Call` carrying `name="hermes-auto"` |

## The `session_id` trap — covered at both ends

`agent/auxiliary_client.py:7006` passes **no** `session_id` (confirmed by reading it);
`chat_completion_helpers.py:2097` and `transports/chat_completions.py:601` pass a possibly-`None` one.
All three call forms are parametrized tests.
`test_empty_string_session_id_would_have_failed_the_schema` demonstrates that `session_id or ""` trips
`minLength` — the failure a `session_id='s-1'`-only suite cannot see.

## Decisions a reviewer should know

1. **`supports_health_check=True`, `models_url=""`.** `hermes_cli/doctor.py:2128-2176` probes
   `models_url or base_url + "/models"` with `Authorization: Bearer $HERMES_AUTO_ROUTER_TOKEN`, and
   **returns early without probing when the var is unset**, so it never produces a spurious failure for
   an unconfigured user. `models_url` left empty so the address derives from `base_url` and cannot drift.
2. **Kwargs used**: `name`, `display_name`, `description`, `api_mode`, `base_url`, `env_vars`,
   `fallback_models`, `default_aux_model="auto:balanced"`, `supports_vision=True`,
   `supports_health_check=True`. `default_aux_model` is load-bearing — without it auxiliary tasks fall
   back to the main model.
3. **Envelope values from Hermes are clamped to 256 chars** (the schema's cap). A long session id would
   otherwise 400 every request on our *own* schema. Clamped identically in both copies; the drift test
   includes 5000-char inputs.
4. **`uninstall` on an unmarked file raises `InstallError`** rather than returning `False`. The plan left
   this unspecified; deleting is clobbering, and "never clobber a file this project did not write" is a
   stated must-have.
5. **`render()` takes the template as its first argument**, so `install` names `SHIM_SOURCE` at the call
   site honestly rather than via a decorative comment, and the tests need no module-global monkeypatch.
6. **`build_profile()` lets `ModuleNotFoundError` propagate.** A `ProviderProfile` has no meaning outside
   Hermes; returning a stand-in would let a caller believe it had a registrable profile.
7. **Shim is ASCII-only, including `MARKER`.** It executes in a foreign runtime; a codepage issue there
   is invisible until discovery silently drops the provider.

## Environment facts recorded

- **`$HERMES_HOME`**: `C:\Users\<user>\AppData\Local\hermes` (via `LOCALAPPDATA`)
- **Install target**: `…\hermes\plugins\model-providers\hermes-auto\__init__.py`
- The checkout at `…\hermes\hermes-agent\` is a **sibling** of the target, not a parent — so the install
  is plugin data and never a core modification. Pinned by
  `test_installer_never_touches_the_hermes_checkout_itself`.
- **Zero Hermes modifications confirmed**: `git status --porcelain` in the checkout is empty;
  `…\hermes\plugins\` does not exist; `test_no_test_wrote_into_the_real_hermes_home` guards it in CI.

## Duplication contract for 02-07's `doctor`

`provider.build_envelope` and `template.SHIM_SOURCE`'s inline `build_extra_body` are the two copies.
Drift is caught by 8 parametrized comparisons plus a declarative-field comparison. **`doctor` should
additionally compare the installed file's `PLUGIN_VERSION` against `hermes_auto.version.__version__`**
to detect a stale on-disk shim — the installed file carries the version that wrote it.

## Issues raised (real, out of scope here)

- **`pyproject.toml` needed no change** — `packages.find` auto-discovered `hermes_auto.hermes_shim`.
  Confirmed by building a wheel, not assumed: all three shim modules and `provider.py` are packaged,
  schema count still 8.
- **`02-CONTEXT.md` § VERIFIED HERMES FACTS item 2 should be sharpened** before 02-07 reads it — see the
  registration finding above. Applied in commit alongside this summary.
- **`plugin.yaml` is not required for model-provider discovery** — `_import_plugin_dir` returns early
  only when `__init__.py` is missing and never reads the manifest, though every bundled plugin ships
  one. Not written. If a future `hermes plugins list` surface wants it, that is a 02-07 concern.
