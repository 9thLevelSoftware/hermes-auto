# Plan 01-05 Summary — Hermes Compatibility Pinning & CI

**Status**: Complete
**Executed**: 2026-07-26
**Phase**: 01-contracts-schemas-baselines, Wave 2
**Requirements**: R26 (primary), R25 (nightly-compatibility-CI portion only)

---

## Files Created

| Path | Provides |
|------|----------|
| `src/hermes_auto/compatibility.py` | Supported Hermes range + fail-closed compatibility probe (174 lines) |
| `tests/unit/test_compatibility.py` | 16 tests: in-range, out-of-range, unparseable, pre-release, absent, CalVer regression |
| `.github/workflows/ci.yml` | Cross-platform test matrix + `schema-validation` phase-close gate (82 lines) |
| `.github/workflows/hermes-compat.yml` | Nightly compatibility against latest release and development branch (156 lines) |

Nothing outside `files_modified` was created or edited. `pyproject.toml`, `tests/conftest.py`,
`tests/contract/`, `docs/`, `scripts/`, and `design.md` are untouched by this plan.

---

## Distribution Name — Confirmed, Not Assumed

`HERMES_DISTRIBUTION_NAME = "hermes-agent"`. Confirmed before writing a line of code, on this machine:

```
$ python -c "import importlib.metadata as m; print(m.version('hermes-agent'))"
0.19.0

$ python -c "import importlib.metadata as m; m.version('hermes')"
importlib.metadata.PackageNotFoundError: No package metadata was found for hermes
```

Corroborated on disk:
`C:/Users/dasbl/AppData/Local/hermes/hermes-agent/venv/Lib/site-packages/hermes_agent-0.19.0.dist-info/`

`"hermes"` is **not** a Hermes distribution. Shortening the constant would make the probe report
`HERMES_NOT_INSTALLED` forever, silently disabling the gate this module exists to provide.
`test_distribution_name_is_hermes_agent` is the regression guard.

### Interpreter note — why the detection gate needed the machine default

`hermes-agent` is deliberately **absent from the project `.venv`**: PROJECT.md requires sidecar
dependencies stay isolated from the Hermes Agent environment, and this plan is forbidden from adding
Hermes as a dependency. So the two probe checks were run against different interpreters, and both are
correct:

| Interpreter | `detect_hermes_version()` | `check_compatibility()` |
|---|---|---|
| `./.venv/Scripts/python.exe` (project, isolated) | `None` | `HERMES_NOT_INSTALLED`, `is_usable=False` |
| machine default (Hermes Agent venv) — **read-only, nothing installed** | `0.19.0` | `COMPATIBLE`, `is_usable=True` |

This is the strongest available evidence: the probe returns `COMPATIBLE` against a **real** Hermes
0.19.0 install and fails closed where Hermes is absent. Nothing was installed into the machine default.

**This did not trigger the stop gate.** The gate fires if `version("hermes-agent")` raises *on this
machine*; it does not, so the resolved fact holds.

---

## Version Range — Derivation

`SUPPORTED_HERMES = ">=0.19,<1.0"`

- **Lower bound `>=0.19` is evidence-based, not invented.** `NousResearch/hermes-agent` declares
  `version = "0.19.0"` in its own `pyproject.toml`; PyPI publishes `hermes-agent==0.19.0`; the local
  install reports `0.19.0`. Three independent sources, identical.
- **Upper bound `<1.0` is a deliberate project decision.** design.md states no version bounds
  anywhere — §10.2 Tier 4 says only "Explicit supported-version range" and §18 Phase 0 item 3 says only
  "Pin supported Hermes versions". `<1.0` assumes a 1.0 release is where Hermes would break the
  internal modules the Tier-4 bridge reaches into. That is a judgement call and should be revisited
  when Hermes approaches 1.0, not treated as inherited fact.
- Declared as **one module-level constant**, so widening support is a one-line change plus a test
  update (recorded in an inline comment alongside the CHANGELOG requirement).

---

## The CalVer Trap and How It Is Guarded

Hermes's git **tags** are CalVer (`v2026.7.20`, `v2026.7.7.2`, `v2026.6.19`). Its **distribution
version** is semver `0.19.0`. `importlib.metadata` reports `0.19.0`, never `2026.7.20`.

A maintainer who reads the releases page may conclude the pin is wrong and "fix" it to `>=2026.1`.
That specifier matches nothing, so the probe would report `UNSUPPORTED_VERSION` for every real Hermes
install — silently disabling the gate while every test that only checks "the probe returns a status"
stays green.

Guarded in **three** places:

1. A block comment above `SUPPORTED_HERMES` in `compatibility.py` stating the trap explicitly.
2. `test_calver_tag_is_not_treated_as_a_version` feeds the real tag `"2026.7.20"` and asserts
   `UNSUPPORTED_VERSION`. The CalVer "fix" therefore breaks a test **with the explanation attached in
   the docstring**, rather than failing silently.
3. `test_supported_range_is_a_valid_specifier` catches a typo in the constant immediately.

---

## Fail-Closed Design

`COMPATIBLE` is returned only after a successful `packaging.version.Version` parse **and** a
`SpecifierSet.contains()` match. There is no permissive fallback, no "assume compatible if unsure"
branch, and no override environment variable.

| Condition | Status | `is_usable` |
|---|---|---|
| In range (incl. pre-release like `0.20.0rc1`) | `COMPATIBLE` | True |
| Outside range (`0.18.9`, `1.0.0`, `2026.7.20`) | `UNSUPPORTED_VERSION` | False |
| Unparseable (`"not-a-version"`, `""`, `"latest"`) | `VERSION_UNREADABLE` | False |
| Distribution absent | `HERMES_NOT_INSTALLED` | False |

Two deliberate design choices worth preserving:

- **`is_usable` is `status is COMPATIBLE`**, not a "not in this set of bad statuses" test. A status
  added later defaults to *not* usable — the fail-closed direction.
  `test_no_status_other_than_compatible_is_usable` iterates the enum, so a future permissive status
  fails a test instead of quietly widening what counts as usable.
- **`detect_hermes_version()` catches only `PackageNotFoundError`.** A corrupt dist-info or a
  permissions error propagates rather than being misreported as a clean standalone environment.
- **`prereleases=True`** on `.contains()` — `SpecifierSet` excludes pre-releases by default, which
  would reject a Hermes the bridge can actually work with. Guarded by
  `test_prerelease_inside_range_is_compatible`.

`HERMES_NOT_INSTALLED` is **information, not an error** — the gateway runs standalone per §11.3. It is
still not *usable*, because this type answers the **bridge** question, not the gateway question.

`compatibility.py` imports only `dataclasses`, `enum`, `importlib.metadata`, and `packaging`. No
`hermes` import, no intra-package import — so it loads before any other subsystem initializes, which is
what makes a *startup* probe possible.

---

## Workflows

### `ci.yml`
- `test` job: `fail-fast: false`, 3 OSes (`ubuntu-latest`, `macos-latest`, `windows-latest`) x Python
  3.11/3.12 = 6 cells. `fail-fast: false` so a Windows-only failure cannot hide a macOS-only one.
- Top-level `permissions: contents: read` and `concurrency` with `cancel-in-progress: true`.
- `schema-validation` job: one unconditional assertion, no `continue-on-error`, no tolerance clause.
- **No linter** — none exists in `pyproject.toml`, and adding one would require editing a frozen file.
  Lint tooling is Phase 11 (R25). The `expected_artifacts` text was already corrected in the plan.

### `hermes-compat.yml`
- `schedule` (`0 6 * * *`) + `workflow_dispatch`.
- `latest-release`: **not** `continue-on-error` — a break against the supported release is a real
  failure. `hermes-main`: `continue-on-error: true` — a break in unreleased Hermes is signal, not a
  broken build.
- Hermes installs are **gated on repository variables**, never hardcoded. `HERMES_DIST_NAME` and
  `HERMES_GIT_URL` were **provisioned 2026-07-27**, and `hermes-agent` is genuinely on PyPI at 0.19.0,
  so both jobs really install and really measure. The indirection stays because the distribution name
  and URL are upstream-controlled: a rename must be fixable without editing a workflow.
- `report` job (`if: always()`) writes a two-row markdown table to `$GITHUB_STEP_SUMMARY`, then
  **fails (exit 1) if either variable is empty**. Without that step, deleting a variable would make the
  nightly green while testing nothing — vacuously satisfying both the Phase 1 criterion "CI runs against
  latest release and current `main`" and the Phase 11 criterion "nightly compatibility CI against
  Hermes `main` is green". A gate that cannot fail is not a gate.
- No credential, token, or password literal. The only secret-shaped references are `vars.*`, which are
  non-secret repository variables (Hermes is a public MIT repository).

### Three hardening choices made during implementation

These are additions to the plan's letter, each fixing a real failure mode:

1. **`cache-dependency-path: pyproject.toml`** on every `setup-python` step. This repository has no
   `requirements.txt`, and `setup-python`'s pip cache resolves `requirements.txt` first. Naming the
   file keeps the cache key deterministic rather than depending on fallback order.
2. **`needs['hermes-main'].result`** bracket notation instead of `needs.hermes-main.result`. In GitHub
   expressions a hyphen in dot-property access is ambiguous with subtraction; bracket indexing is
   unambiguous and documented.
3. **`if` statements instead of `[ -z "$X" ] && missing=...`** in the report guard. GitHub Actions runs
   `run:` steps under `bash -e`, where a trailing `&&` whose test is *false* returns non-zero and aborts
   the step — meaning the guard would have failed spuriously in exactly the healthy case. Verified
   under `bash -e` in all four states (both vars set -> rc 0; one missing -> rc 1; install gate empty ->
   rc 0 skip; install gate set -> proceeds).

`vars.*` values are passed through `env:` rather than interpolated into shell text, so a variable value
cannot alter the command.

---

## Verification Record

**All 10 plan-level `verification_commands` pass. All 33 task-level `> verification:` lines pass. 0 failures, 0 fixes needed.**

Notable outputs:

```
$ ./.venv/Scripts/python.exe -m pytest tests/unit/test_compatibility.py -q
16 passed in 0.62s

$ ./.venv/Scripts/python.exe -m pytest tests/ -q          # full suite, all Wave-2 plans landed
56 passed in 2.68s

$ PYTHONPATH=src python -c "... check_compatibility()"     # machine default, real Hermes present
CompatibilityResult(status=COMPATIBLE, detected_version='0.19.0',
                    supported_range='>=0.19,<1.0', detail='hermes-agent 0.19.0 satisfies >=0.19,<1.0.')

$ git rev-parse HEAD:design.md
18bb54b36485fa0813ec67f84a74628a9eee3aae     # matches the 01-CONTEXT.md pin; citations valid as read
```

Beyond the required gates, three additional checks were run because YAML-embedded shell is a common
silent-failure surface:

- **Heredoc integrity after YAML dedent** — extracted the `run:` block via `yaml.safe_load` and
  asserted the `PY` terminator lands at column 0 once the block scalar is dedented. A terminator left
  indented is a parse error that only shows up at runtime.
- **The probe body executes** — ran the exact heredoc contents through the interpreter.
- **The `schema-validation` step's exact command** — `load_schemas()` now returns **7** schemas, so the
  phase-close gate is confirmed well-formed, not merely syntactically valid.

---

## Handoff

### Should Hermes's `requires-python = ">=3.11,<3.14"` be mirrored in Phase 7?

**Recommendation: no — do not copy the upper bound onto this project's `requires-python`.**

This project currently declares `requires-python = ">=3.11"` with no upper bound. Mirroring Hermes's
ceiling would make `hermes-auto-router` uninstallable on Python 3.14 for **every** user, including the
majority who never enable the bridge. design.md §10.2 is explicit that the Tier-4 bridge "should remain
optional", and §11.3 has the gateway running standalone. A whole-distribution constraint to serve an
optional feature inverts that.

The correct mechanism is an **optional extra** in Phase 7:

```toml
[project.optional-dependencies]
hermes-bridge = ["hermes-agent>=0.19,<1.0"]
```

pip then enforces Hermes's own `requires-python` transitively, at exactly the right scope and
automatically — it stays correct if upstream widens to 3.14 without anyone editing this repository.
Note also that `requires-python` upper bounds are baked into published metadata and cannot be relaxed
retroactively for already-released versions, which is why they are widely discouraged for libraries.

The runtime probe delivered here is the second half of that answer: on a 3.14 environment with no
compatible Hermes, `check_compatibility()` returns `HERMES_NOT_INSTALLED` or `UNSUPPORTED_VERSION` and
`is_usable` is False, so the bridge declines cleanly instead of failing at import.

**Related gap for Phase 7/11**: Hermes supports up to 3.13, but the CI matrix tests only 3.11 and 3.12.
Adding a `"3.13"` cell would close the gap between what this project claims to support and what it
actually exercises. It was not added here because the plan fixed the matrix at two versions.

### Deferred to Phase 11 (R25 packaging and release automation)

Named explicitly so none of it is mistaken for an oversight:

1. **Lint tooling and a lint job.** No linter exists in `pyproject.toml` (frozen), and `ci.yml`
   correctly claims none.
2. **Publish / release / deploy workflow** — PyPI publishing, preferably via OIDC trusted publishing so
   no long-lived token is ever stored.
3. **Coverage gate and coverage upload** — needs a secret this repository has not provisioned.
4. **Wheel-content check as a CI job.** The 7-schema-inside-a-built-wheel assertion currently lives only
   in the `01-CONTEXT.md` phase-close gate and is run by hand. `package-data` globs are a classic silent
   packaging failure: the schemas could stop shipping while every in-repo `PYTHONPATH=src` gate stayed
   green. This deserves to be a standing CI job, not a one-time manual check.
5. **Action pinning by commit SHA.** Both workflows pin `actions/checkout@v4` and
   `actions/setup-python@v5` by **major tag**, as the plan required. Major tags are mutable; SHA pinning
   plus Dependabot is the supply-chain-hardened form.
6. **Artifact integrity** — checksums/attestations for published wheels.

### Notes for whoever touches this next

- `.venv` has no Hermes and must stay that way. Any check needing a real Hermes present must be run
  against the machine default interpreter, **read-only** — never install into it.
- The `schema-validation` job asserts `>= 7` schemas. If a later phase adds an eighth, the job keeps
  passing; if the loader silently returns `{}`, it fails. That asymmetry is intentional.
