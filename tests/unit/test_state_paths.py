"""Unit tests for per-OS state directory resolution.

Every test here pins ``HERMES_AUTO_STATE_DIR`` to a ``tmp_path``. That is not
tidiness: ``state_dir()`` creates directories, so a test that let the default
``~/.hermes/auto-router`` through would write into the developer's real install
and would pass or fail depending on what was already there.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from hermes_auto.state import paths


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Point the state directory at a scratch path for every test in this module."""
    monkeypatch.setenv(paths.STATE_DIR_ENV_VAR, str(tmp_path / "state"))


def test_env_override_wins_and_creates_the_directory(tmp_path: pathlib.Path) -> None:
    resolved = paths.state_dir()

    assert resolved == (tmp_path / "state").resolve()
    assert resolved.is_dir()


def test_env_override_beats_a_configured_value(tmp_path: pathlib.Path) -> None:
    """The override exists so a test process cannot touch a real install.

    If a configured value could beat it, a config.yaml picked up from the
    developer's HERMES_HOME would silently redirect the whole test suite.
    """
    resolved = paths.state_dir(configured=str(tmp_path / "from-config"))

    assert resolved == (tmp_path / "state").resolve()
    assert not (tmp_path / "from-config").exists()


def test_configured_value_is_used_when_no_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.delenv(paths.STATE_DIR_ENV_VAR, raising=False)

    resolved = paths.state_dir(configured=str(tmp_path / "from-config"))

    assert resolved == (tmp_path / "from-config").resolve()
    assert resolved.is_dir()


def test_default_is_the_design_documented_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """design.md 16 pins the default at ``~/.hermes/auto-router``.

    HOME/USERPROFILE are redirected so this asserts the real default expression
    without creating anything under the developer's actual home directory.
    """
    monkeypatch.delenv(paths.STATE_DIR_ENV_VAR, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    assert paths.DEFAULT_STATE_DIR == "~/.hermes/auto-router"
    assert paths.state_dir(create=False) == (
        fake_home / ".hermes" / "auto-router"
    ).resolve()


def test_create_false_does_not_touch_the_filesystem(tmp_path: pathlib.Path) -> None:
    """Asking "where is the state?" must not have the side effect of creating it.

    ``read_runtime`` and ``read_token`` both resolve paths on the "is anything
    running?" path; if resolution created an install, a status check on a clean
    machine would leave one behind.
    """
    resolved = paths.state_dir(create=False)

    assert resolved == (tmp_path / "state").resolve()
    assert not resolved.exists()


@pytest.mark.parametrize(
    ("func", "expected_leaf"),
    [
        (paths.runtime_dir, paths.RUNTIME_DIR_NAME),
        (paths.log_dir, paths.LOG_DIR_NAME),
    ],
)
def test_subdirectories_are_created_under_the_state_dir(func, expected_leaf: str) -> None:
    resolved = func()

    assert resolved.name == expected_leaf
    assert resolved.parent == paths.state_dir(create=False)
    assert resolved.is_dir()


def test_token_path_is_a_file_path_whose_parent_exists() -> None:
    """``create`` refers to the containing directory, never to the token itself.

    Creating the token file belongs to ``mint_token``, which is the only code
    that knows how to create it with restrictive permissions from the first byte.
    """
    resolved = paths.token_path()

    assert resolved.name == paths.TOKEN_FILE_NAME
    assert resolved.parent.is_dir()
    assert not resolved.exists()


def test_directory_creation_is_idempotent() -> None:
    first = paths.runtime_dir()
    (first / "sentinel").write_text("kept", encoding="utf-8")

    second = paths.runtime_dir()

    assert first == second
    assert (second / "sentinel").read_text(encoding="utf-8") == "kept"


def test_tilde_and_env_vars_expand_in_the_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("HERMES_AUTO_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv(
        paths.STATE_DIR_ENV_VAR, os.path.join("$HERMES_AUTO_TEST_ROOT", "expanded")
    )

    assert paths.state_dir(create=False) == (tmp_path / "expanded").resolve()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are a no-op on Windows")
def test_created_directory_is_owner_only_on_posix() -> None:
    """0o700 is a real guarantee here and only here.

    On Windows this assertion is skipped rather than weakened, because
    ``os.chmod`` does not touch Windows ACLs: after ``chmod(p, 0o600)`` the mode
    still reads 0o666. The token file's Windows protection is an ``icacls`` grant
    verified by readback in ``hermes_auto.gateway.auth``, not by this mode.
    """
    resolved = paths.state_dir()

    assert resolved.stat().st_mode & 0o777 == paths.DIR_MODE


def test_module_imports_no_third_party_package() -> None:
    """The provider shim runs inside the Hermes venv and may import only stdlib.

    Asserting on the module's own source rather than on ``sys.modules`` matters:
    a ``sys.modules`` check passes trivially whenever the test session happens
    not to have imported the package yet, which makes it no check at all.
    """
    source = pathlib.Path(paths.__file__).read_text(encoding="utf-8")
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    ]

    assert import_lines == ["import os", "import pathlib"], import_lines
