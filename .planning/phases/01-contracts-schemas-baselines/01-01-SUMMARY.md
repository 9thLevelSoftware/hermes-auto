# Plan 01-01 Summary — Repository Skeleton & Packaging Contract

**Status**: Complete
**Executed**: 2026-07-26
**Phase**: 01-contracts-schemas-baselines, Wave 1
**Requirement**: R26

---

## Environment (authoritative for every later Phase 1 plan)

| Fact | Value |
|------|-------|
| Resolved Python | **3.11.15** (`main, Jun 23 2026, 15:20:37`) [MSC v.1944 64 bit (AMD64)] |
| `.venv` absolute path | `C:\Users\dasbl\hermes-auto\.venv` |
| `.venv` `sys.prefix` | `C:\Users\dasbl\hermes-auto\.venv` |
| **Interpreter later plans MUST use** | **`C:\Users\dasbl\hermes-auto\.venv\Scripts\python.exe`** |
| Machine default `sys.prefix` (do NOT install into) | `C:\Users\dasbl\AppData\Local\hermes\hermes-agent\venv` |
| Isolation assertion | PASSED — `'hermes-agent' not in sys.prefix` |

The isolation assertion ran **before** any install, per PROJECT.md's constraint that sidecar
dependencies stay isolated from the Hermes Agent environment. Nothing was installed into the machine
default interpreter.

From bash, invoke as `./.venv/Scripts/python.exe`. All verification commands in this phase are POSIX
shell and must be run through the Bash tool, never PowerShell.

---

## Dependency Set (final — `pyproject.toml` is forbidden to plans 01-03, 01-04, 01-05, 01-07)

**Runtime** (`[project].dependencies`):

| Declared | Consumer |
|----------|----------|
| `packaging>=23.0` | `compatibility.py` version-range parsing (plan 01-05) |
| `jsonschema>=4.20` | schema validation tests (plans 01-03, 01-04) |
| `pyyaml>=6.0` | baseline corpus loader (plan 01-07) |

**Dev** (`[project.optional-dependencies].dev`):

| Declared | Consumer |
|----------|----------|
| `pytest>=8.0` | all test plans |
| `pytest-asyncio>=0.23` | async gateway tests (Phase 2+) |
| `hypothesis>=6.98` | property tests (design.md §20.2) |

**No linter was added** (`ruff`, `flake8`, `black`, `mypy` are all absent) — lint tooling arrives in
Phase 11 per R25. Plan 01-05's CI workflow must not claim a lint job.

No HTTP framework, HTTP client, ONNX runtime, or LiteLLM was added. Those arrive in Phase 2 and later.

**Resolved versions installed into `.venv`** (evidence, not pins):

```
attrs==26.1.0             jsonschema==4.26.0              pytest==9.1.1
colorama==0.4.6           jsonschema-specifications==2025.9.1  pytest-asyncio==1.4.0
hypothesis==6.161.5       packaging==26.2                 PyYAML==6.0.3
iniconfig==2.3.0          pluggy==1.6.0                   referencing==0.37.0
Pygments==2.20.0          rpds-py==2026.6.3               sortedcontainers==2.4.0
                          typing_extensions==4.16.0
```

Note: `pytest` resolved to **9.1.1** and `pytest-asyncio` to **1.4.0**, both well above the declared
floors. `pytest-asyncio` 1.x requires an explicit `asyncio_mode` or per-test `@pytest.mark.asyncio`
marker; no async tests exist yet, so nothing is affected in Phase 1. Phase 2 must decide the mode.

---

## Wheel-Content Check

**Result: PASSED.**

```
$ .venv/Scripts/python.exe -m pip wheel --no-deps -w dist-check .
$ python -c "... assert 'hermes_auto/py.typed' in namelist"
PY_TYPED_SHIPPED OK
```

The built wheel `hermes_auto_router-0.1.0-py3-none-any.whl` contains all eleven subpackage
`__init__.py` files, `hermes_auto/version.py`, `hermes_auto/__init__.py`, and `hermes_auto/py.typed`.
`dist-check/` was deleted afterward and is covered by `.gitignore` (an explicit `dist-check/` entry
was added — the `dist/` entry alone does NOT match it).

### This check MUST be re-run after plans 01-03 and 01-04 land

The current wheel carries **no** `data/schema/**/*.json` files, because no schema files exist yet.
`package-data` globs are a common silent packaging failure: if the glob does not match, `load_schemas()`
returns `{}` from an installed copy while every in-repo `PYTHONPATH=src` gate stays green. The
phase-close gate in `01-CONTEXT.md` already asserts `len(schemas) == 7` inside a built wheel — run it.

**Pre-verified for you.** To de-risk that gate, the globs were probed empirically in a throwaway copy
of the project outside the repository (the real `src/hermes_auto/data/schema/` is forbidden to this
plan). A dummy `data/schema/probe.schema.json` and a dummy `data/harness-profiles.yaml` were added to
the copy and a wheel built:

```
data payload in wheel: ['hermes_auto/data/harness-profiles.yaml',
                        'hermes_auto/data/schema/probe.schema.json']
PACKAGE_DATA_GLOBS OK
```

Both `data/schema/**/*.json` (recursive `**`) and `data/*.yaml` resolve correctly under the installed
setuptools, and files under the non-package `data/schema/` subdirectory are attributed to the
`hermes_auto` package as declared. **The declared globs are correct; no `pyproject.toml` edit is
needed by 01-03 or 01-04.** The scratch copy was deleted.

---

## Subpackage Names vs. design.md §17

**No differences.** All eleven subpackages match design.md §17 exactly:

`gateway`, `routing`, `state`, `inventory`, `health`, `adapters`, `harnesses`, `telemetry`,
`evaluation`, `learning`, `data`

Each `__init__.py` contains only a one-line docstring naming the subpackage's responsibility and the
phase that implements it. No `__all__`, no imports, no routing/scoring/eligibility/cost/candidate-
selection logic anywhere. The only import statement in the entire `src/hermes_auto/` tree is the
required `from .version import __version__` in the top-level `__init__.py`.

### Deliberately NOT created (correct, not a gap)

design.md §17 also lists seven top-level modules. None were created and their absence must not
trigger a stop gate in any later plan:

| Module | Owner |
|--------|-------|
| `plugin.py`, `provider.py` | Phase 2 |
| `commands.py`, `cli.py` | Phase 2/7 |
| `config.py`, `supervisor.py` | Phase 2+ |
| `compatibility.py` | **Plan 01-05, this phase** |

Also not created by this plan: `docs/` (plan 01-02, same wave), `.github/` and
`src/hermes_auto/compatibility.py` (plan 01-05), `src/hermes_auto/gateway/schemas.py` (plan 01-03),
`src/hermes_auto/data/schema/` (plans 01-03/01-04), `scripts/` (plan 01-07).

---

## Files Created

**Packaging and meta**
- `pyproject.toml` — `hermes-auto-router`, src-layout discovery, dynamic version from
  `hermes_auto.version.__version__`, full Phase 1 dependency set, package-data globs, pytest config
  (`testpaths=["tests"]`, `pythonpath=["src"]`, `contract` and `performance` markers)
- `.gitignore` — build/cache/venv artifacts only; verified NOT to exclude `design.md`, `docs/`, or
  `.planning/`; includes an explicit `dist-check/` entry
- `README.md` — description, design.md §1 ASCII architecture diagram, `## Status`, `## Install`,
  `## Documentation` (forward links to `docs/architecture.md`, `docs/threat-model.md`,
  `docs/privacy.md` — created by 01-02 and 01-06)
- `LICENSE` — full Apache License 2.0, 201 lines, "Copyright 2026 9th Level Software"
- `SECURITY.md` — supported-versions table, disclosure process with `TODO: security contact`
  placeholder, and the four hard invariants (loopback-only binding, generated bearer token with
  restrictive file permissions, no CORS, no raw prompt/secret retention by default)
- `CHANGELOG.md` — Keep a Changelog format, `## [Unreleased]` / `### Added` /
  "Repository skeleton and packaging contract"

**Package tree**
- `src/hermes_auto/__init__.py`, `version.py` (`__version__: str = "0.1.0"`), `py.typed`
- `src/hermes_auto/{gateway,routing,state,inventory,health,adapters,harnesses,telemetry,evaluation,learning,data}/__init__.py`

**Test tree**
- `tests/__init__.py`, `tests/conftest.py`
- `tests/{unit,property,contract,integration,e2e,fault,performance}/__init__.py`
- `tests/fixtures/.gitkeep`
- `tests/unit/test_package_layout.py`

---

## Fixtures Available to Downstream Plans

`tests/conftest.py` provides two **module-scoped** fixtures:

| Fixture | Returns | Notes |
|---------|---------|-------|
| `repo_root` | `pathlib.Path(__file__).resolve().parent.parent` | repository root |
| `schema_dir` | `repo_root / "src" / "hermes_auto" / "data" / "schema"` | **does not assert existence** — the directory does not exist in Wave 1; plans 01-03 and 01-04 create it |

---

## Verification Record

26 verification commands executed, 26 exited 0, 0 failed.

| Gate | Result |
|------|--------|
| Task 1 (`pyproject.toml`, `.gitignore`) | 7/7 pass |
| Task 2 (`src/` tree, meta files) | 11/11 pass |
| Task 3 (`.venv`, `tests/` tree) | 8/8 pass |
| Plan-level `verification_commands` | all pass |
| `.venv/Scripts/python.exe -m pip install -e ".[dev]"` | rc=0 |
| `.venv/Scripts/python.exe -m pytest tests/ -q` | **3 passed in 1.59s** |
| Wheel contains `hermes_auto/py.typed` | PASS |
| `git diff --exit-code design.md` | rc=0, unchanged (blob `18bb54b3…` matches the pin) |
| No file outside `files_modified` created or modified | confirmed via `git status --short -uall` |

`design.md` blob verified against the `01-CONTEXT.md` pin
(`18bb54b36485fa0813ec67f84a74628a9eee3aae`) **before** execution began; section citations §1, §5.4,
§17, §18, §20, §23 are therefore valid as read.

---

## Notes and Risks for Downstream Plans

1. **Use `C:\Users\dasbl\hermes-auto\.venv\Scripts\python.exe` for every python/pip/pytest call.**
   The machine default is the Hermes Agent venv; installing there violates PROJECT.md's isolation
   constraint.
2. **Do not edit `pyproject.toml`.** The dependency set is complete for Phase 1. If a plan believes it
   needs another dependency, it must emit `BLOCKED` naming the dependency rather than adding it.
3. **`schema_dir` does not assert existence.** Plans 01-03/01-04 must create the directory themselves.
4. **`pytest-asyncio` resolved to 1.x.** Phase 2 must set `asyncio_mode` in `[tool.pytest.ini_options]`
   or mark async tests explicitly. No Phase 1 impact.
5. **Re-run the wheel-content check after 01-03 and 01-04.** See the section above — the globs are
   pre-verified correct, but the 7-schema count assertion still needs to run for real.
6. `src/hermes_auto_router.egg-info/` is generated by the editable install. It is gitignored via
   `*.egg-info/` and is not part of this plan's deliverable.
