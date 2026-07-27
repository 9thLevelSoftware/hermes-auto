"""Unit tests for token minting, permissions, and constant-time comparison.

The permission tests assert on the *effective* permission read back from the
operating system, never on the fact that a call was made. That distinction is the
whole point on Windows, where ``os.chmod(p, 0o600)`` returns successfully and
changes nothing: measured here, the mode still reads 0o666 afterwards.
"""

from __future__ import annotations

import ast
import os
import pathlib
import string
import subprocess

import pytest

from hermes_auto.gateway import auth
from hermes_auto.gateway.auth import (
    AuthError,
    compare_token,
    mint_token,
    permissions_ok,
    read_token,
    secure_write,
    token_permissions_ok,
)
from hermes_auto.state.paths import STATE_DIR_ENV_VAR, token_path

# Windows allocates a fresh console window for a console-subsystem child when the
# parent has no console of its own. CREATE_NO_WINDOW suppresses it; it is absent on
# POSIX, hence getattr.
_NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """No test may mint into the developer's real ``~/.hermes/auto-router``."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path / "state"))


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def test_minted_token_is_long_and_url_safe() -> None:
    token = mint_token()

    assert len(token) >= 32
    allowed = set(string.ascii_letters + string.digits + "-_")
    assert set(token) <= allowed, set(token) - allowed


def test_two_mints_differ() -> None:
    assert mint_token() != mint_token()


def test_minting_is_high_entropy() -> None:
    """Weak randomness here is indistinguishable from a strong token by eye."""
    assert len({mint_token() for _ in range(64)}) == 64
    assert auth.TOKEN_ENTROPY_BYTES == 32


def test_mint_then_read_round_trips() -> None:
    token = mint_token()

    assert read_token() == token


def test_remint_replaces_rather_than_appends() -> None:
    mint_token()
    second = mint_token()

    assert read_token() == second
    assert token_path(create=False).read_bytes() == second.encode("ascii")


def test_token_file_has_no_trailing_newline() -> None:
    """The file is read by the stdlib-only Hermes shim; exact bytes matter."""
    token = mint_token()

    assert token_path(create=False).read_bytes() == token.encode("ascii")


# ---------------------------------------------------------------------------
# Reading: absent is not corrupt
# ---------------------------------------------------------------------------


def test_read_token_on_a_missing_file_returns_none() -> None:
    assert read_token() is None


def test_reading_a_missing_token_does_not_create_one() -> None:
    read_token()

    assert not token_path(create=False).exists()


def test_empty_token_file_raises_rather_than_reading_as_absent() -> None:
    """An empty token would otherwise authenticate an empty Authorization header."""
    mint_token()
    token_path(create=False).write_bytes(b"")

    with pytest.raises(AuthError, match="empty"):
        read_token()


def test_non_ascii_token_file_raises() -> None:
    mint_token()
    token_path(create=False).write_bytes(b"\xff\xfe not a token")

    with pytest.raises(AuthError):
        read_token()


def test_surrounding_whitespace_is_stripped_on_read() -> None:
    """An editor that adds a trailing newline must not break authentication."""
    mint_token()
    token_path(create=False).write_bytes(b"  abc123  \n")

    assert read_token() == "abc123"


# ---------------------------------------------------------------------------
# Constant-time comparison
# ---------------------------------------------------------------------------


def test_equal_tokens_compare_true() -> None:
    token = mint_token()

    assert compare_token(token, token) is True


def test_differing_tokens_compare_false() -> None:
    assert compare_token("alpha", "beta") is False


def test_correct_prefix_wrong_suffix_compares_false() -> None:
    """The case a prefix oracle would leak, and the reason for compare_digest."""
    token = mint_token()

    assert compare_token(token[:-1] + "x", token) is False
    assert compare_token(token[:-1], token) is False
    assert compare_token(token + "x", token) is False


def test_empty_supplied_token_compares_false() -> None:
    assert compare_token("", mint_token()) is False
    assert compare_token("", "") is True


@pytest.mark.parametrize("bad", [None, 123, b"bytes", ["list"]])
def test_non_string_input_is_a_failed_auth_not_an_exception(bad: object) -> None:
    """A malformed header must not 500, which would distinguish it from "wrong"."""
    assert compare_token(bad, "expected") is False  # type: ignore[arg-type]
    assert compare_token("supplied", bad) is False  # type: ignore[arg-type]


def test_comparison_uses_compare_digest_not_equality() -> None:
    """A one-line difference no reviewer catches by reading the code.

    Asserted against the parsed AST rather than a substring, so it cannot be
    satisfied by the word appearing in a comment or a docstring.
    """
    tree = ast.parse(pathlib.Path(auth.__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "compare_token"
    )

    calls = {
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    comparisons = [node for node in ast.walk(function) if isinstance(node, ast.Compare)]

    assert "compare_digest" in calls
    assert not any(
        isinstance(op, (ast.Eq, ast.NotEq))
        for node in comparisons
        for op in node.ops
        if _compares_a_token(node)
    ), "token equality must go through compare_digest"


def _compares_a_token(node: ast.Compare) -> bool:
    names = {
        child.id for child in ast.walk(node) if isinstance(child, ast.Name)
    }
    return bool(names & {"supplied", "expected"})


# ---------------------------------------------------------------------------
# Permissions: read back, never assumed
# ---------------------------------------------------------------------------


def test_permissions_are_ok_immediately_after_minting() -> None:
    """The end-to-end guarantee on whatever platform this is running on."""
    mint_token()

    ok, reason = token_permissions_ok()

    assert ok is True, reason
    assert reason


def test_permissions_report_false_for_a_missing_file() -> None:
    ok, reason = token_permissions_ok()

    assert ok is False
    assert "does not exist" in reason


def test_permissions_never_raise_for_an_unverifiable_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing icacls degrades to a report, never to an outage.

    doctor surfaces the condition; refusing to start would push the user to run
    the gateway some other way, which is strictly less safe.
    """
    mint_token()
    monkeypatch.setattr(auth.shutil, "which", lambda _name: None)

    ok, reason = token_permissions_ok()

    if os.name == "nt":
        assert ok is False
        assert "icacls" in reason
    else:
        assert ok is True, reason


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL behavior")
def test_chmod_alone_would_not_have_protected_the_token() -> None:
    """The measurement that justifies this module's whole Windows branch.

    ``os.chmod(p, 0o600)`` succeeds on Windows and changes nothing: the mode still
    reads 0o666 and the ACL is untouched. A POSIX-style implementation would have
    reported success and left the token readable. If this test ever fails,
    Windows has changed and the icacls branch should be re-examined -- it does not
    mean the assertion is wrong.
    """
    plain = pathlib.Path(token_path(create=True).parent) / "chmod-only"
    plain.write_bytes(b"not-a-real-token")
    os.chmod(plain, 0o600)

    assert plain.stat().st_mode & 0o777 == 0o666

    ok, reason = permissions_ok(plain)
    assert ok is False, "a chmod-only file must not pass the ACL readback"
    assert reason


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL behavior")
def test_readback_rejects_a_file_with_inherited_permissions() -> None:
    """Proves the readback fails when it should, not merely that it passes.

    A check that only ever runs against a correctly-secured file cannot tell a
    working implementation from one that returns True unconditionally.
    """
    directory = token_path(create=True).parent
    inherited = directory / "inherited"
    inherited.write_bytes(b"not-a-real-token")

    ok, reason = permissions_ok(inherited)

    assert ok is False
    assert reason


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL behavior")
def test_readback_sees_a_widened_acl() -> None:
    """Grant Everyone and confirm the readback notices."""
    mint_token()
    target = token_path(create=False)

    completed = subprocess.run(
        ["icacls", str(target), "/grant", "*S-1-1-0:R"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    if completed.returncode != 0:
        pytest.skip(f"could not widen the ACL to test the readback: {completed.stderr}")

    ok, reason = token_permissions_ok()

    assert ok is False
    assert reason


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_posix_mode_is_owner_only() -> None:
    mint_token()

    assert token_path(create=False).stat().st_mode & 0o777 == auth.FILE_MODE


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_posix_readback_rejects_a_widened_mode() -> None:
    mint_token()
    target = token_path(create=False)
    os.chmod(target, 0o644)

    ok, reason = token_permissions_ok()

    assert ok is False
    assert "0644" in reason


# ---------------------------------------------------------------------------
# secure_write: the shared primitive plans 02-02 and 02-06 reuse
# ---------------------------------------------------------------------------


def test_secure_write_is_reusable_for_any_secret(tmp_path: pathlib.Path) -> None:
    """02-02's salt and 02-06's admin token call this rather than re-deriving it."""
    target = tmp_path / "nested" / "salt"

    secure_write(target, b"per-install-salt")

    assert target.read_bytes() == b"per-install-salt"
    ok, reason = permissions_ok(target)
    assert ok is True, reason


def test_secure_write_replaces_a_pre_existing_file(tmp_path: pathlib.Path) -> None:
    """A pre-created token path must not be written into under someone else's ACL."""
    target = tmp_path / "token"
    target.write_bytes(b"planted-by-someone-else")

    secure_write(target, b"fresh")

    assert target.read_bytes() == b"fresh"
    ok, reason = permissions_ok(target)
    assert ok is True, reason


def test_secure_write_raises_authError_not_a_bare_oserror(
    tmp_path: pathlib.Path,
) -> None:
    """A directory where a file is expected must surface as a typed error."""
    target = tmp_path / "occupied"
    target.mkdir()

    with pytest.raises(AuthError):
        secure_write(target, b"data")


# ---------------------------------------------------------------------------
# The token value never reaches a log
# ---------------------------------------------------------------------------


def test_module_makes_no_logging_calls_at_all() -> None:
    """Stronger than "no logging call takes the token as an argument".

    Auditing every call's arguments is a check that decays the moment someone
    introduces a helper. Asserting the module logs nothing whatsoever is a
    property a reviewer can confirm at a glance and a refactor cannot erode. If
    this module ever needs diagnostics, log the path and the boolean result from
    the caller -- never from here.
    """
    source = pathlib.Path(auth.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "logging" not in imported

    logging_methods = {"debug", "info", "warning", "error", "exception", "critical"}
    offenders = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in logging_methods
    ]
    assert offenders == [], offenders

    prints = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert prints == []


def test_errors_name_the_path_and_never_the_token() -> None:
    token = mint_token()
    token_path(create=False).write_bytes(b"")

    with pytest.raises(AuthError) as caught:
        read_token()

    assert token not in str(caught.value)
    assert str(token_path(create=False)) in str(caught.value)
