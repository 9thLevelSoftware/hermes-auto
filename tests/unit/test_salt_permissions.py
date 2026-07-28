"""The salt gets the bearer token's permission treatment, readback included.

``gateway/auth.py``'s docstring states the rule this module tests:

    ``secure_write`` and ``permissions_ok`` are deliberately generic and public:
    they are the single implementation of per-platform restrictive creation and
    readback for the whole project. [...] Three copies of platform permission
    logic would drift, and the two that drifted would be the two nobody
    re-verified.

``telemetry/redaction.py`` re-derived that logic instead of calling it, and it
had already drifted in exactly the predicted way: ``auth`` tries the
domain-qualified ``DOMAIN\\user`` principal before the bare name, ``redaction``
only ever used the bare name -- and, decisively, ``redaction`` performed **no
readback at all**. ``icacls`` returning 0 is not evidence that the ACL is
narrow; ``auth`` knows this and re-reads, ``redaction`` did not.

Why the salt is worth the same protection as the token, despite not being a
credential: the salt is the only thing making ``root_session_hash``
non-correlatable. An unsalted ``sha256(session_id)`` satisfies the same
``^[0-9a-f]{64}$`` pattern and is stable across every machine, which is the
property the salt exists to destroy. An attacker who can *read* the salt can
precompute the digest of any session id they can guess, and session ids are not
secret. So a readable salt silently converts every hashed identifier back into a
correlatable one, with no visible change in the log.
"""

from __future__ import annotations

import pathlib

import pytest

from hermes_auto.gateway import auth
from hermes_auto.telemetry import redaction
from hermes_auto.telemetry.redaction import (
    SALT_FILENAME,
    RedactionError,
    install_salt,
)


@pytest.fixture(autouse=True)
def _isolate_salt_cache(tmp_path: pathlib.Path):
    """The salt cache is process-wide; a leaked entry would mask a real failure."""
    redaction._salt_cache.clear()
    yield
    redaction._salt_cache.clear()


def test_salt_creation_goes_through_the_shared_secure_write(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not "writes a 0o600 file" -- *calls the one implementation*.

    Asserting on the resulting mode would pass against a second private copy of
    the logic, which is the thing that drifted. The call itself is the property
    worth pinning, because it is what makes a future fix to ``secure_write``
    reach the salt too.
    """
    calls: list[pathlib.Path] = []
    real = auth.secure_write

    def spy(path: pathlib.Path, data: bytes) -> None:
        calls.append(path)
        real(path, data)

    monkeypatch.setattr(auth, "secure_write", spy)

    install_salt(tmp_path)

    assert calls == [tmp_path / SALT_FILENAME]


def test_salt_permissions_are_read_back_after_writing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permission that has not been read back has not been set."""
    checked: list[pathlib.Path] = []
    real = auth.permissions_ok

    def spy(path: pathlib.Path) -> tuple[bool, str]:
        checked.append(path)
        return real(path)

    monkeypatch.setattr(auth, "permissions_ok", spy)

    install_salt(tmp_path)

    assert checked == [tmp_path / SALT_FILENAME]


def test_a_failed_permission_readback_raises_and_removes_the_salt(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unlink is the load-bearing half, and it is why this cannot warn.

    A salt left on disk with permissions that could not be verified is worse
    than no salt, because the *next* call takes the ``path.exists()`` branch,
    reads it back, and trusts it -- so a single unverifiable write would poison
    every subsequent digest on that install, silently and permanently. Removing
    it keeps the failure loud instead of letting it cure itself on retry.
    """
    monkeypatch.setattr(
        auth, "permissions_ok", lambda path: (False, "ACL grants Everyone:(F)")
    )

    with pytest.raises(RedactionError) as caught:
        install_salt(tmp_path)

    message = str(caught.value)
    assert "Everyone" in message, "the readback's reason must reach the operator"
    assert not (tmp_path / SALT_FILENAME).exists(), (
        "an unverifiable salt must not survive; the next call would trust it"
    )


def test_a_freshly_created_salt_passes_its_own_readback(
    tmp_path: pathlib.Path,
) -> None:
    """End to end on the real platform, with nothing monkeypatched.

    This is the test that would have caught the drift: it asserts the salt file
    satisfies the *same* checker the token file must satisfy, on whichever
    platform the suite is running on.
    """
    install_salt(tmp_path)

    ok, reason = auth.permissions_ok(tmp_path / SALT_FILENAME)

    assert ok, f"freshly created salt failed its own permission readback: {reason}"


def test_doctor_reports_salt_permissions(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``doctor`` checked both token files and never the salt.

    A permission control nobody can observe is a permission control that has
    already failed silently once and nobody noticed. ``doctor`` is where the
    operator learns the state of the other two; the salt belongs beside them.
    """
    from hermes_auto.commands import run_doctor

    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(tmp_path))
    install_salt(tmp_path)

    names = [check.name for check in run_doctor()]

    assert "salt permissions" in names
