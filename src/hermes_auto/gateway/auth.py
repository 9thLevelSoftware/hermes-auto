"""Bearer-token minting, storage permissions, and constant-time comparison.

`docs/threat-model.md` names this module as the owner of two rows of
`design.md` 21: *Local port accessed by another process* (Spoofing) and *Secret
leakage in logs* (Information disclosure). Loopback binding stops remote callers
but does nothing about local ones, so on a shared machine the token file's
permissions are the entire access control on the user's provider budget.

Four properties are load-bearing.

**The restrictive mode is set at creation, never by a later chmod.** On POSIX the
file is opened with ``O_CREAT | O_EXCL | O_WRONLY`` and mode ``0o600``, so it
never exists world-readable even momentarily. ``O_EXCL`` over a prior ``unlink``
also means that if another process has pre-created the token path, the write
fails loudly instead of quietly filling in a file whose ACL an attacker chose.

**On Windows, permissions are an ACL question and ``os.chmod`` cannot answer
it.** Measured on the development machine: after ``os.chmod(p, 0o600)`` the mode
still reads ``0o666``, and the file's real ACL still granted five principals
including ``BUILTIN\\Administrators`` and a machine-local group. So Windows gets
``icacls /inheritance:r /grant:r`` and -- decisively -- the effective ACL is
**read back**. A permission that has not been read back is a permission that has
not been set; this module never reports otherwise.

**Comparison is constant-time.** ``==`` on a token short-circuits at the first
differing byte, leaking a prefix oracle to a local process that can time
responses. ``hmac.compare_digest`` does not. This is a one-line difference no
reviewer catches by reading, which is why it is asserted by a test.

**The token value is never logged.** This module makes no logging calls at all --
not at debug, not truncated, not hashed. That is enforced by a test which parses
the AST, because "we agreed not to log it" is exactly the kind of convention that
survives review and not refactoring. Exceptions raised here name the *path*, and
never the value.

``secure_write`` and ``permissions_ok`` are deliberately generic and public: they
are the single implementation of per-platform restrictive creation and readback
for the whole project. Plan 02-02's per-install salt and plan 02-06's admin token
call these rather than re-deriving ``os.open(..., 0o600)`` and ``icacls``. Three
copies of platform permission logic would drift, and the two that drifted would
be the two nobody re-verified.
"""

from __future__ import annotations

import getpass
import hmac
import os
import pathlib
import re
import secrets
import shutil
import subprocess

from ..state.paths import token_path

# secrets.token_urlsafe(32) draws 32 random bytes and base64url-encodes them,
# yielding 43 characters of ~256-bit entropy. The argument is bytes of entropy,
# not output length; do not "round it to 32 characters".
TOKEN_ENTROPY_BYTES: int = 32

# Owner read/write, nothing else. A real guarantee on POSIX only; see module
# docstring for what stands in for it on Windows.
FILE_MODE: int = 0o600

_ICACLS_TIMEOUT_SECONDS: float = 15.0

# Matches one ACE line of `icacls <path>` output, e.g. `DEVILS-WORKTOP\dasbl:(F)`
# or `NT AUTHORITY\SYSTEM:(I)(F)`. Structural rather than English, so it survives
# a localized Windows: only the `PRINCIPAL:(RIGHTS)` shape is assumed.
_ACE_RE = re.compile(r"^(?P<principal>.+?):(?P<rights>(?:\([^)]*\))+)\s*$")


class AuthError(Exception):
    """A token could not be minted, read, or protected."""


# ---------------------------------------------------------------------------
# The single per-platform implementation of restrictive creation and readback
# ---------------------------------------------------------------------------


def _current_user() -> str:
    """The account name to grant on Windows."""
    return os.environ.get("USERNAME") or getpass.getuser()


def _run_icacls(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke ``icacls`` without a shell and without flashing a console window."""
    if shutil.which("icacls") is None:
        raise FileNotFoundError("icacls not found on PATH")
    # CREATE_NO_WINDOW keeps a supervised, detached sidecar from popping a
    # console window on every mint. Absent on non-Windows, hence getattr.
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        ["icacls", *args],
        capture_output=True,
        text=True,
        timeout=_ICACLS_TIMEOUT_SECONDS,
        check=False,
        creationflags=creation_flags,
    )


def _restrict_windows_acl(path: pathlib.Path) -> None:
    """Strip inherited ACEs and grant the current user alone.

    ``/inheritance:r`` removes the inherited entries -- on this machine that is
    SYSTEM, Administrators, and two machine-local groups -- and ``/grant:r``
    replaces rather than adds, so re-minting cannot accumulate grants.
    """
    user = _current_user()
    domain = os.environ.get("USERDOMAIN")
    # Domain-qualified first: on a machine where a local and a domain account
    # share a name, the bare form is ambiguous. Bare form is the fallback and is
    # known to resolve correctly when USERDOMAIN is absent.
    principals = [f"{domain}\\{user}"] if domain else []
    principals.append(user)

    last_error = ""
    for principal in principals:
        try:
            completed = _run_icacls(
                [str(path), "/inheritance:r", "/grant:r", f"{principal}:F"]
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            # Not fatal: the caller verifies with permissions_ok(), and doctor
            # reports the result. A gateway that refused to start because icacls
            # was missing would be less safe, not more -- the user would run it
            # some other way.
            last_error = str(exc)
            break
        if completed.returncode == 0:
            return
        last_error = (completed.stderr or completed.stdout).strip()
    if last_error:
        # Deliberately silent about the failure here; permissions_ok() is the
        # authority and is what callers must consult.
        return


def secure_write(path: pathlib.Path, data: bytes) -> None:
    """Write ``data`` to ``path`` with owner-only access from the first byte.

    The project's single implementation. POSIX gets the mode at ``open`` time;
    Windows gets an ``icacls`` grant immediately after creation.

    A residual Windows exposure is recorded rather than papered over: between
    ``os.open`` and the ``icacls`` call the file exists with inherited ACLs.
    Closing that window needs a security descriptor supplied at creation, which
    requires ``pywin32`` -- a dependency this phase refuses. The window is
    microseconds inside a directory that is itself not world-writable, and
    ``permissions_ok`` proves the end state. Do not claim it is closed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Remove any pre-existing file so O_EXCL is meaningful. Without this, a
    # token path an attacker created first would be opened and written into,
    # handing them the token under their own ACL.
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AuthError(f"{path}: could not be replaced: {exc}") from exc

    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE
        )
    except OSError as exc:
        raise AuthError(f"{path}: could not be created securely: {exc}") from exc

    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise AuthError(f"{path}: could not be written: {exc}") from exc

    if os.name == "nt":
        _restrict_windows_acl(path)


def _posix_permissions_ok(path: pathlib.Path) -> tuple[bool, str]:
    mode = path.stat().st_mode & 0o777
    if mode == FILE_MODE:
        return True, f"mode {mode:04o} (owner read/write only)"
    return False, (
        f"mode {mode:04o} grants access beyond the owner; expected "
        f"{FILE_MODE:04o}"
    )


def _windows_permissions_ok(path: pathlib.Path) -> tuple[bool, str]:
    """Read the effective ACL back and require that only the owner appears.

    An allowlist, not a denylist. A denylist of ``Everyone``/``Users``/``BUILTIN``
    would have passed on this machine's default ACL, which also granted a
    machine-local ``CodexSandboxUsers`` group that no denylist would have named.
    Requiring that every ACE resolve to the current account is the check that
    actually corresponds to "only I can read this token".
    """
    try:
        completed = _run_icacls([str(path)])
    except FileNotFoundError:
        return False, "icacls is not available on PATH; ACL could not be verified"
    except subprocess.TimeoutExpired:
        return False, "icacls did not complete; ACL could not be verified"
    except OSError as exc:
        return False, f"icacls could not be run ({exc}); ACL could not be verified"

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return False, f"icacls failed: {detail[0] if detail else 'unknown error'}"

    # The path is echoed on the first line before the first ACE; remove it so the
    # ACE parser sees a uniform `PRINCIPAL:(RIGHTS)` on every line.
    output = completed.stdout.replace(str(path), "", 1)

    expected = _current_user().casefold()
    aces: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = _ACE_RE.match(line.strip())
        if match:
            aces.append((match["principal"].strip(), match["rights"]))

    if not aces:
        return False, "icacls returned no access-control entries to verify"

    foreign = [
        principal
        for principal, _ in aces
        if principal.rsplit("\\", 1)[-1].casefold() != expected
    ]
    if foreign:
        return False, (
            "token file is accessible to " + ", ".join(sorted(foreign)) + "; "
            f"expected only {_current_user()}"
        )

    inherited = [principal for principal, rights in aces if "(I)" in rights]
    if inherited:
        return False, (
            "inherited permissions are still present for "
            + ", ".join(sorted(inherited))
            + "; /inheritance:r did not take effect"
        )

    return True, f"ACL grants only {_current_user()} ({len(aces)} entry set)"


def permissions_ok(path: pathlib.Path) -> tuple[bool, str]:
    """Read back the effective permissions of ``path``.

    Returns ``(ok, reason)`` and never raises for an unverifiable environment:
    a missing ``icacls`` yields ``(False, reason)`` so ``doctor`` can report it
    while the gateway still runs. Raising here would convert a diagnostic gap
    into an outage.
    """
    if not path.exists():
        return False, f"{path} does not exist"
    try:
        if os.name == "nt":
            return _windows_permissions_ok(path)
        return _posix_permissions_ok(path)
    except OSError as exc:
        return False, f"permissions could not be read: {exc}"


# ---------------------------------------------------------------------------
# Token surface
# ---------------------------------------------------------------------------


def mint_token(configured: str | os.PathLike[str] | None = None) -> str:
    """Generate a new bearer token, store it restrictively, and return it.

    Does not raise when the permission readback fails: on a machine without
    ``icacls`` the gateway must still start, and ``token_permissions_ok`` plus
    ``doctor`` are how that condition is surfaced. Callers that require a
    verified-restrictive token must check ``token_permissions_ok()`` themselves.
    """
    token = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    secure_write(token_path(configured), token.encode("ascii"))
    return token


def read_token(configured: str | os.PathLike[str] | None = None) -> str | None:
    """Return the stored token, or ``None`` if none has been minted.

    Absent is not corrupt, the same distinction the runtime file draws: no file
    means "not set up yet", while a file that exists and is empty or undecodable
    means something damaged it and is an ``AuthError``.
    """
    path = token_path(configured, create=False)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthError(f"{path}: exists but cannot be read: {exc}") from exc

    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise AuthError(
            f"{path}: does not contain an ASCII token; re-mint it"
        ) from exc

    if not token:
        raise AuthError(f"{path}: is empty; re-mint it")
    return token


def compare_token(supplied: str, expected: str) -> bool:
    """Constant-time bearer-token comparison.

    ``hmac.compare_digest`` because ``==`` returns as soon as two bytes differ,
    which lets a local process recover the token one byte at a time by timing
    responses. Returns ``False`` rather than raising for non-string or
    non-encodable input, so a malformed Authorization header is a failed auth and
    not a 500 that distinguishes "malformed" from "wrong" to an attacker.
    """
    if not isinstance(supplied, str) or not isinstance(expected, str):
        return False
    try:
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except (UnicodeEncodeError, TypeError):
        return False


def token_permissions_ok(
    configured: str | os.PathLike[str] | None = None,
) -> tuple[bool, str]:
    """Read back the effective permissions of the bearer-token file."""
    return permissions_ok(token_path(configured, create=False))


__all__ = [
    "TOKEN_ENTROPY_BYTES",
    "FILE_MODE",
    "AuthError",
    "secure_write",
    "permissions_ok",
    "mint_token",
    "read_token",
    "compare_token",
    "token_permissions_ok",
]
