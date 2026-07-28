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


def _accepted_principals() -> set[str]:
    """Case-folded principal spellings that denote *this* account, and no other.

    ``icacls`` echoes the **resolved** principal, so the spelling in its output
    is not necessarily the spelling that was granted: a local account granted
    bare comes back as ``COMPUTERNAME\\user``. All three forms below are
    therefore accepted.

    What is *not* accepted is a qualifier naming some other authority. Comparing
    only the leaf after the final backslash -- which is what this check used to
    do -- treats ``CORP\\dasbl`` and ``OTHERBOX\\dasbl`` as the current account,
    which is exactly the ambiguity :func:`_restrict_windows_acl` grants the
    domain-qualified form first in order to avoid. A verifier that discards the
    qualifier undoes that.

    Comparing by SID would be stronger still and needs no environment variables,
    but reading a SID requires ``pywin32`` or parsing ``whoami /user`` -- a
    dependency this phase refuses and a subprocess on a hot path respectively.
    The qualified-string comparison closes the gap that was actually reachable.
    """
    user = _current_user()
    accepted = {user.casefold()}
    # USERDOMAIN is the logon authority; COMPUTERNAME is what a local account
    # resolves to. On a workgroup machine they are equal, and on a domain-joined
    # one a local account still resolves to COMPUTERNAME.
    for qualifier in (os.environ.get("USERDOMAIN"), os.environ.get("COMPUTERNAME")):
        if qualifier:
            accepted.add(f"{qualifier}\\{user}".casefold())
    return accepted


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
    ``os.open`` and the ``icacls`` call completing, the file exists with
    inherited ACLs and the token already written into it. Closing that window
    needs a security descriptor supplied at creation, which requires
    ``pywin32`` -- a dependency this phase refuses. ``permissions_ok`` proves the
    end state. Do not claim the window is closed.

    **The window is milliseconds, not microseconds.** An earlier version of this
    docstring said microseconds, which understated it by about four orders of
    magnitude and made the residual sound like a formality. The token is not
    merely created before ``icacls`` runs -- it is written, flushed **and
    fsynced**, and ``os.fsync`` on Windows is ``_commit()``, a real disk
    barrier. Measured on the development machine: ``open``+``write``+``flush``+
    ``fsync`` alone is a median of 16 ms (range 6-31 ms), and the whole
    ``secure_write`` including the ``icacls`` process spawn is a median of
    188 ms (range 109-462 ms). A local process polling the state directory has
    a wide window in which to read the file.

    What bounds the exposure is the *directory*, not the timing: the state
    directory is created with ``mode=0o700``, which on Windows blocks ACL
    inheritance, so a directory this project created does not grant
    ``Authenticated Users`` anything to exercise during the window. That is a
    real control rather than an assumption -- see ``state/paths._ensure_dir``,
    which explains why the ``mode`` argument is load-bearing there, and
    ``doctor``'s "state directory permissions" check, which reports the case the
    project does *not* control: a pre-existing directory it deliberately does
    not re-narrow.
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

    accepted = _accepted_principals()
    aces: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = _ACE_RE.match(line.strip())
        if match:
            aces.append((match["principal"].strip(), match["rights"]))

    if not aces:
        return False, "icacls returned no access-control entries to verify"

    # The *whole* principal, not `rsplit("\\", 1)[-1]`. Matching on the leaf
    # accepts a same-named account from any other authority.
    foreign = [
        principal for principal, _ in aces if principal.casefold() not in accepted
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


#: Principals that may appear on a *directory* without meaning that an ordinary
#: local account can write into it. SYSTEM and Administrators can reach anything
#: on the machine regardless of any ACL, so excluding them would report a risk
#: that no ACL change could remove. ``OWNER RIGHTS`` and ``CREATOR OWNER`` denote
#: the owner, which is the account this process runs as.
_BENIGN_DIRECTORY_PRINCIPALS = frozenset(
    {
        "nt authority\\system",
        "builtin\\administrators",
        "owner rights",
        "creator owner",
    }
)


def directory_permissions_ok(path: pathlib.Path) -> tuple[bool, str]:
    """Report whether *path* is writable only by this account and the system.

    Separate from :func:`permissions_ok` because a directory legitimately
    carries principals a *token file* must not: a directory created with
    ``mode=0o700`` still shows SYSTEM, Administrators and OWNER RIGHTS, and
    requiring "only the current user" there would report every correct install
    as broken.

    Why a directory's ACL is worth checking at all, given the token file has its
    own: on Windows ``FILE_DELETE_CHILD`` on a directory permits deleting a file
    **regardless of that file's own DACL**. ``admin.py`` re-reads ``admin-token``
    on every request by design, so a local principal who can write the state
    directory can *substitute* the admin token and hold the admin scope -- the
    token's own permissions never enter into it.

    ``state.paths`` narrows directories it creates (``os.mkdir``'s ``mode``
    argument is honoured on Windows and blocks inheritance), but deliberately
    does **not** re-narrow a directory that already existed, so that an operator
    who widened one on purpose is not silently overridden. This function is how
    that decision stays visible instead of merely unenforced.

    POSIX returns the mode check, where the same reasoning applies to the write
    bit for group and other.
    """
    if not path.exists():
        return False, f"{path} does not exist"
    if not path.is_dir():
        return False, f"{path} is not a directory"

    if os.name != "nt":
        try:
            mode = path.stat().st_mode & 0o777
        except OSError as exc:
            return False, f"permissions could not be read: {exc}"
        if mode & 0o022:
            return False, (
                f"mode {mode:04o} lets group or other write into the state "
                f"directory, which permits replacing the token files inside it"
            )
        return True, f"mode {mode:04o} (not group- or world-writable)"

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

    output = completed.stdout.replace(str(path), "", 1)
    accepted = _accepted_principals() | _BENIGN_DIRECTORY_PRINCIPALS

    foreign: list[str] = []
    for line in output.splitlines():
        match = _ACE_RE.match(line.strip())
        if match:
            principal = match["principal"].strip()
            if principal.casefold() not in accepted:
                foreign.append(principal)

    if foreign:
        return False, (
            "state directory is writable by " + ", ".join(sorted(set(foreign)))
            + "; a principal who can write this directory can replace the token "
            "files inside it regardless of their own permissions"
        )
    return True, f"ACL grants only {_current_user()} and the system accounts"


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
    "directory_permissions_ok",
    "mint_token",
    "read_token",
    "compare_token",
    "token_permissions_ok",
]
