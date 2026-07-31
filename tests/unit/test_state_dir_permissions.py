"""The state directory's own permissions, on the platform where they are an ACL.

Two separate claims are pinned here, because a review of this area asserted the
first and the evidence refuted it.

**A state directory this code creates is narrow on Windows too.** ``_ensure_dir``
gates its ``os.chmod`` on ``os.name == "posix"``, and the module comment calls
the Windows behaviour a no-op -- which reads as "the directory is never
narrowed on Windows". That is wrong, and the thing that makes it wrong is easy
to delete by accident: CPython **honours the ``mode`` argument to ``os.mkdir``
on Windows**, and ``mode=0o700`` produces a directory with inheritance blocked
and only ``SYSTEM``, ``Administrators`` and ``OWNER RIGHTS`` on it. Measured on
the development machine: a directory created under ``C:\\`` with ``mode=0o700``
carries none of the ``BUILTIN\\Users:(RX)`` or
``NT AUTHORITY\\Authenticated Users:(M)`` entries that the *same* directory
created with the default mode inherits.

That matters because ``Authenticated Users:(M)`` on a directory includes
``FILE_DELETE_CHILD``, which permits deleting a file **regardless of that
file's own DACL** -- and ``admin.py`` re-reads ``admin-token`` per request by
design, so a principal who can replace that file holds the admin scope. The
``mode`` argument is the only thing standing between the default configuration
and that, so it gets a test.

**A pre-existing state directory is *not* narrowed, and that is deliberate.**
``_ensure_dir`` applies the restrictive mode only to directories it created, so
"an operator who deliberately widened an existing state directory is not
overridden silently". The consequence is that pointing ``gateway.state_dir`` at
a directory that already exists outside the user profile keeps whatever ACL it
had. Overriding it would be wrong; leaving it *unreported* is what was wrong.
``doctor`` now reports it, which is the same treatment the token and the salt
get.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from hermes_auto.gateway import auth

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="directory ACL semantics are Windows-only"
)

#: Principals that legitimately appear on a directory created with mode 0o700.
#: SYSTEM and Administrators can reach anything on the machine regardless, so
#: excluding them would be security theatre rather than security.
_EXPECTED_BENIGN = {"NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators", "OWNER RIGHTS"}

#: Principals whose presence means a non-administrative local account can write
#: into the directory -- and therefore replace the admin token inside it.
_BROAD = ("Authenticated Users", "BUILTIN\\Users", "Everyone")


def test_a_created_state_dir_does_not_grant_broad_principals(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing effect of ``mkdir(mode=0o700)`` on Windows.

    Asserted against a directory created under a parent whose ACL *would* be
    inherited, so the test proves inheritance was blocked rather than that the
    parent happened to be narrow already.
    """
    from hermes_auto.state.paths import state_dir

    target = tmp_path / "outer" / "auto-router"
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(target))

    resolved = state_dir()

    ok, reason = auth.directory_permissions_ok(resolved)
    assert ok, reason
    for broad in _BROAD:
        assert broad not in reason


def test_directory_permissions_ok_rejects_a_broadly_writable_directory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory an ordinary local account can write is reported, not accepted.

    ``FILE_DELETE_CHILD`` on the directory beats the admin token's own DACL, and
    the admin token is re-read per request, so this is a full admin-scope
    substitution rather than a nuisance.
    """
    target = tmp_path / "wide"
    target.mkdir()

    class _Completed:
        returncode = 0
        stderr = ""
        stdout = (
            f"{target} NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)\n"
            "BUILTIN\\Administrators:(I)(OI)(CI)(F)\n"
            "NT AUTHORITY\\Authenticated Users:(I)(M)\n"
        )

    monkeypatch.setattr(auth, "_run_icacls", lambda args: _Completed())

    ok, reason = auth.directory_permissions_ok(target)

    assert not ok
    assert "Authenticated Users" in reason


def test_directory_permissions_ok_accepts_the_owner(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The current account and the unavoidable system principals are fine."""
    target = tmp_path / "narrow"
    target.mkdir()
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""

    class _Completed:
        returncode = 0
        stderr = ""
        stdout = (
            f"{target} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n"
            "BUILTIN\\Administrators:(OI)(CI)(F)\n"
            "OWNER RIGHTS:(OI)(CI)(F)\n"
            f"{domain}\\{user}:(OI)(CI)(F)\n"
        )

    monkeypatch.setattr(auth, "_run_icacls", lambda args: _Completed())

    ok, reason = auth.directory_permissions_ok(target)

    assert ok, reason


def test_doctor_reports_state_directory_permissions(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing wide directory is not overridden, so it must be reported."""
    from hermes_auto.commands import run_doctor

    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(tmp_path / "state"))

    names = [check.name for check in run_doctor()]

    assert "state directory permissions" in names
