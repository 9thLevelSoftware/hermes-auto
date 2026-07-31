"""The Windows ACL allowlist must compare the *qualified* principal, not its leaf.

``_windows_permissions_ok``'s docstring states the property it is meant to
enforce:

    Requiring that every ACE resolve to the current account is the check that
    actually corresponds to "only I can read this token".

No resolution happened. The comparison was
``principal.rsplit("\\\\", 1)[-1].casefold() != expected`` -- the leaf after the
final backslash -- so ``CORP\\dasbl``, ``OTHERBOX\\dasbl`` and ``LOCALBOX\\dasbl``
all satisfied "the current account". That is precisely the ambiguity
``_restrict_windows_acl`` was written to disambiguate: it grants the
domain-qualified principal *first* because "on a machine where a local and a
domain account share a name, the bare form is ambiguous", and then the verifier
threw the qualifier away.

Scope, stated honestly. This is a narrower weakness than the fail-closed
branches it sits beside, and those branches are correct: a broad ``Everyone:(F)``
grant *is* rejected, an inherited ACE *is* rejected, and an empty ACE list *is*
rejected. Reaching this specific gap needs an ACE granting a same-named
principal from another authority, which is a domain-joined or
machine-renamed scenario rather than a drive-by. It is fixed because a verifier
that is documented to resolve accounts and does not is worse than one that never
claimed to -- the docstring is what the next reader will trust.

The accepted set is deliberately wider than one string: ``icacls`` echoes the
*resolved* principal, which is ``COMPUTERNAME\\user`` for a local account even
when the grant was issued unqualified. Rejecting that would fail every
freshly-minted token on a workgroup machine -- and since the salt now raises on
a failed readback, it would refuse to start rather than merely warn.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from hermes_auto.gateway import auth

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="ACL semantics are Windows-only"
)


class _Completed:
    """Stand-in for ``subprocess.CompletedProcess`` with just what is read."""

    returncode = 0
    stderr = ""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


@pytest.fixture
def acl_says(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """Feed crafted ``icacls`` output to the verifier and return its verdict.

    The ACE text is what is under test, so it is supplied directly rather than
    produced by granting real permissions -- a real grant cannot create an ACE
    for a principal from a domain this machine is not joined to, which is the
    whole case being checked.
    """
    target = tmp_path / "token"
    target.write_bytes(b"x")

    def run(*ace_lines: str) -> tuple[bool, str]:
        stdout = f"{target} " + "\n".join(ace_lines) + "\n"
        monkeypatch.setattr(auth, "_run_icacls", lambda args: _Completed(stdout))
        return auth.permissions_ok(target)

    return run


def test_a_foreign_domain_qualifier_is_rejected(acl_says) -> None:
    """``CORP\\dasbl`` is not ``DEVILS-WORKTOP\\dasbl``, however it is spelled."""
    user = os.environ.get("USERNAME") or ""
    ok, reason = acl_says(f"CORP\\{user}:(F)")
    assert not ok
    assert "CORP" in reason


def test_another_machines_qualifier_is_rejected(acl_says) -> None:
    """A same-named account on a different machine is a different account."""
    user = os.environ.get("USERNAME") or ""
    ok, _ = acl_says(f"OTHERBOX\\{user}:(F)")
    assert not ok


def test_the_real_local_principal_is_accepted(acl_says) -> None:
    """The form ``icacls`` actually emits on this machine must still pass.

    Without this the tightening becomes a denial of service: the salt now
    *raises* on a failed readback, so a verifier that rejects the legitimate
    principal refuses to start rather than warning.
    """
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    ok, reason = acl_says(f"{domain}\\{user}:(F)")
    assert ok, reason


def test_the_computername_qualifier_is_accepted(acl_says) -> None:
    """A local account resolves to ``COMPUTERNAME\\user`` even if granted bare."""
    user = os.environ.get("USERNAME") or ""
    computer = os.environ.get("COMPUTERNAME") or ""
    ok, reason = acl_says(f"{computer}\\{user}:(F)")
    assert ok, reason


def test_an_unqualified_principal_is_accepted(acl_says) -> None:
    """``icacls`` on some configurations echoes the bare name."""
    user = os.environ.get("USERNAME") or ""
    ok, reason = acl_says(f"{user}:(F)")
    assert ok, reason


def test_case_is_still_insensitive(acl_says) -> None:
    """Windows principals are case-insensitive; the fix must not change that."""
    user = (os.environ.get("USERNAME") or "").upper()
    domain = (os.environ.get("USERDOMAIN") or "").lower()
    ok, reason = acl_says(f"{domain}\\{user}:(F)")
    assert ok, reason


# ---------------------------------------------------------------------------
# The fail-closed branches that were already correct, pinned so the tightening
# above cannot regress them.
# ---------------------------------------------------------------------------


def test_a_broad_grant_is_still_rejected(acl_says) -> None:
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    ok, reason = acl_says(f"{domain}\\{user}:(F)", "Everyone:(F)")
    assert not ok
    assert "Everyone" in reason


def test_an_inherited_ace_is_still_rejected(acl_says) -> None:
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    ok, reason = acl_says(f"{domain}\\{user}:(I)(F)")
    assert not ok
    assert "inherited" in reason.lower()


def test_no_aces_at_all_is_still_rejected(acl_says) -> None:
    ok, reason = acl_says("")
    assert not ok
    assert "no access-control entries" in reason
