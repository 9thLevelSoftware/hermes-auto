"""Unit tests for ``hermes_auto.telemetry.redaction``.

``docs/privacy.md`` states that Phase 1 provides **no structural backstop** for
the four prohibitions and that the tests pinning them "land with that code".
This file is those tests, brought forward with the sink.

The load-bearing one is
:func:`test_two_salts_over_one_session_id_diverge`. ``root_session_hash`` is
constrained to ``^[0-9a-f]{64}$``, and an *unsalted* ``sha256(session_id)``
satisfies that pattern exactly as well as a salted one -- it is stable across
every machine running the same session and reversible by enumerating the
session-id space. No schema can see the difference. Divergence under two salts
is the only observable that can.

Every test injects ``directory`` at a ``tmp_path``. Nothing here reads or
writes the real ``~/.hermes/auto-router``.
"""

from __future__ import annotations

import inspect
import logging
import os
import pathlib
import re
import stat
import subprocess
import sys

import pytest

from hermes_auto.telemetry import redaction
from hermes_auto.telemetry.redaction import (
    BANNED_KEYS,
    SALT_FILENAME,
    RedactingFormatter,
    RedactionError,
    get_logger,
    install_salt,
    redact,
    session_digest,
)

#: The pattern Phase 1 froze on ``root_session_hash`` in ``outcome-event.v1``
#: and ``route-decision.v1``. Reproduced literally so a drift in either the
#: schema or the digest shows up here.
FROZEN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

RAW_SESSION_ID = "hermes-session-9f2c4a11-not-a-hash"


@pytest.fixture(autouse=True)
def _isolate_salt_cache(monkeypatch, tmp_path):
    """Keep the process-wide salt cache and the real home directory out of it.

    The cache is keyed by resolved directory, so ``tmp_path`` isolation is
    already sufficient for correctness. Clearing it anyway means a test that
    forgets to pass ``directory`` fails loudly on a missing env var instead of
    silently minting a salt in the developer's home directory.
    """
    redaction._salt_cache.clear()
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(tmp_path / "unused-default"))
    yield
    redaction._salt_cache.clear()


# ---------------------------------------------------------------------------
# The salt
# ---------------------------------------------------------------------------


def test_install_salt_creates_the_file_and_returns_hex(tmp_path):
    value = install_salt(tmp_path)

    assert FROZEN_HASH_PATTERN.fullmatch(value), value
    assert len(bytes.fromhex(value)) == 32

    salt_file = tmp_path / SALT_FILENAME
    assert salt_file.is_file()
    assert salt_file.read_text(encoding="ascii").strip() == value


def test_install_salt_is_idempotent(tmp_path):
    """A second call returns the first salt, not a fresh one.

    If it minted a new salt each time, every digest would be unstable and the
    store could never correlate two events from one session.
    """
    first = install_salt(tmp_path)
    redaction._salt_cache.clear()  # force a real re-read from disk
    second = install_salt(tmp_path)

    assert first == second


def test_install_salt_creates_missing_parent_directories(tmp_path):
    """Absent is not corrupt: a state directory that does not exist yet is made."""
    nested = tmp_path / "does" / "not" / "exist"
    assert not nested.exists()

    install_salt(nested)

    assert (nested / SALT_FILENAME).is_file()


def test_salt_file_permissions_are_restricted(tmp_path):
    """The salt gets the same treatment the bearer token gets.

    On POSIX that is mode ``0o600`` set at creation. On Windows ``os.chmod``
    moves only the read-only bit, so inheritance must be broken with ``icacls``
    and the effective ACL read back -- assuming the write succeeded is exactly
    the failure ``02-CONTEXT.md`` requires ``doctor`` to guard against.

    The readback is performed here rather than imported from plan 02-01's token
    helper: ``src/hermes_auto/state/`` is outside this plan's write scope and
    that module lands in the same wave.
    """
    install_salt(tmp_path)
    salt_file = tmp_path / SALT_FILENAME

    if sys.platform != "win32":
        mode = stat.S_IMODE(salt_file.stat().st_mode)
        assert mode == 0o600, oct(mode)
        return

    completed = subprocess.run(
        ["icacls", str(salt_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    acl = completed.stdout

    # Inheritance broken: no ACE is marked as inherited.
    assert "(I)" not in acl, acl

    account = os.environ.get("USERNAME") or ""
    assert account, "USERNAME must be set for the Windows ACL readback"
    assert account.lower() in acl.lower(), acl

    # No blanket grant to the well-known broad principals.
    for principal in ("Everyone", "BUILTIN\\Users", "AUTHENTICATED USERS"):
        assert principal.lower() not in acl.lower(), (principal, acl)


def test_unwritable_state_directory_raises_rather_than_degrading(tmp_path):
    """No unsalted fallback. A salt that cannot be made is an error.

    The parent of the requested directory is a regular file, so ``mkdir``
    cannot succeed. The contract is that this surfaces as ``RedactionError``
    and not as a digest computed without a salt.
    """
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RedactionError):
        install_salt(blocker / "state")


def test_malformed_salt_file_is_an_error_not_a_regeneration(tmp_path):
    """Absent means create; corrupt means fail.

    Silently replacing a garbled salt would change every future digest without
    saying so, breaking correlation with everything already stored.
    """
    (tmp_path / SALT_FILENAME).write_text("this is not hex", encoding="ascii")

    with pytest.raises(RedactionError):
        install_salt(tmp_path)


def test_short_salt_file_is_rejected(tmp_path):
    """Valid hex of the wrong length is still not a 32-byte salt."""
    (tmp_path / SALT_FILENAME).write_text("abcd", encoding="ascii")

    with pytest.raises(RedactionError):
        install_salt(tmp_path)


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def test_digest_matches_the_frozen_root_session_hash_pattern(tmp_path):
    digest = session_digest(RAW_SESSION_ID, directory=tmp_path)

    assert FROZEN_HASH_PATTERN.fullmatch(digest), digest
    assert digest == digest.lower()
    assert len(digest) == 64


def test_two_salts_over_one_session_id_diverge(tmp_path):
    """The property ``docs/privacy.md`` promises and Phase 1 could not enforce.

    Same session id, two installs, two unrelated digests. Without this the
    field could hold an unsalted ``sha256`` -- identical on every machine, and
    reversible by enumeration -- while validating against the schema perfectly.
    """
    install_a = tmp_path / "install-a"
    install_b = tmp_path / "install-b"

    digest_a = session_digest(RAW_SESSION_ID, directory=install_a)
    digest_b = session_digest(RAW_SESSION_ID, directory=install_b)

    assert digest_a != digest_b
    assert install_salt(install_a) != install_salt(install_b)
    for digest in (digest_a, digest_b):
        assert FROZEN_HASH_PATTERN.fullmatch(digest), digest


def test_digest_is_stable_for_one_salt(tmp_path):
    first = session_digest(RAW_SESSION_ID, directory=tmp_path)
    redaction._salt_cache.clear()  # re-read the salt from disk
    second = session_digest(RAW_SESSION_ID, directory=tmp_path)

    assert first == second


def test_different_session_ids_diverge_under_one_salt(tmp_path):
    assert session_digest("session-a", directory=tmp_path) != session_digest(
        "session-b", directory=tmp_path
    )


def test_digest_is_hmac_not_a_bare_concatenated_hash(tmp_path):
    """Pin the construction, not just the output shape.

    ``sha256(salt + session_id)`` produces an equally well-formed 64-hex string,
    so no assertion on the output can tell the two apart. Recomputing the HMAC
    independently can.
    """
    import hashlib
    import hmac

    salt_hex = install_salt(tmp_path)
    expected = hmac.new(
        bytes.fromhex(salt_hex), RAW_SESSION_ID.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    assert session_digest(RAW_SESSION_ID, directory=tmp_path) == expected

    naive = hashlib.sha256(bytes.fromhex(salt_hex) + RAW_SESSION_ID.encode()).hexdigest()
    assert expected != naive


def test_no_unsalted_digest_path_exists_in_the_source():
    """A source-level assertion, because the fallback would be invisible in output.

    ``hashlib.sha256`` appears exactly once, passed to ``hmac.new`` as the
    digest constructor. A direct ``hashlib.sha256(...)`` *call* would be the
    signature of an unsalted path, so its absence is asserted rather than
    merely checking that the string ``hmac`` appears somewhere.
    """
    source = inspect.getsource(redaction)

    assert "hmac.new(" in source
    assert "hashlib.sha256(" not in source, "a direct sha256() call may be unsalted"


def test_session_digest_rejects_a_non_string(tmp_path):
    """The typed API raises; the never-raising contract belongs to ``redact``."""
    with pytest.raises(TypeError):
        session_digest(1234, directory=tmp_path)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# redact()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("banned", sorted(BANNED_KEYS))
def test_every_banned_key_is_dropped_and_named(banned, tmp_path):
    """Each key on the list, individually. A loop over the constant means a key
    added to ``BANNED_KEYS`` without being handled fails here immediately."""
    event = {banned: "sensitive value", "model": "test-hosted-general"}

    result = redact(event, directory=tmp_path)

    assert banned not in result
    assert result["model"] == "test-hosted-general"
    assert banned in result["_redacted"]
    assert "sensitive value" not in repr(result)


def test_banned_keys_are_matched_case_insensitively(tmp_path):
    """``Authorization`` is the spelling an HTTP header actually uses."""
    result = redact({"Authorization": "Bearer sk-live-abc"}, directory=tmp_path)

    assert "Authorization" not in result
    assert "sk-live-abc" not in repr(result)
    assert "Authorization" in result["_redacted"]


def test_derived_token_counts_survive(tmp_path):
    """Matching is by exact name, not substring, and that is deliberate.

    ``docs/privacy.md`` lists token counts among the things the router
    legitimately keeps. A substring rule on ``token`` or ``prompt`` would
    delete exactly those fields.
    """
    event = {
        "prompt_tokens": 4210,
        "completion_tokens": 260,
        "cached_tokens": 3968,
        "tool_call_count": 2,
    }

    assert redact(event, directory=tmp_path) == event


def test_nested_and_list_nested_banned_keys_are_dropped(tmp_path):
    event = {
        "request": {
            "model": "m",
            "messages": [{"role": "user", "content": "secret prompt"}],
        },
        "candidates": [
            {"id": "cand-a", "api_key": "sk-live-1"},
            {"id": "cand-b", "tool_calls": [{"function": {"arguments": "{}"}}]},
        ],
    }

    result = redact(event, directory=tmp_path)
    rendered = repr(result)

    assert "secret prompt" not in rendered
    assert "sk-live-1" not in rendered
    assert "messages" in result["request"]["_redacted"]
    assert result["request"]["model"] == "m"
    assert result["candidates"][0]["id"] == "cand-a"
    assert "api_key" in result["candidates"][0]["_redacted"]
    assert "tool_calls" in result["candidates"][1]["_redacted"]


def test_redact_is_idempotent(tmp_path):
    for event in (
        {"a": 1},
        {"messages": [{"content": "x"}], "model": "m"},
        {"root_session_id": RAW_SESSION_ID, "nested": {"api_key": "k"}},
        {"_redacted": ["already"], "prompt": "p"},
    ):
        once = redact(event, directory=tmp_path)
        twice = redact(once, directory=tmp_path)
        assert twice == once, event


def test_redact_does_not_mutate_its_input(tmp_path):
    event = {"messages": ["x"], "nested": {"api_key": "k"}}

    redact(event, directory=tmp_path)

    assert event == {"messages": ["x"], "nested": {"api_key": "k"}}


def test_session_id_becomes_the_frozen_hash_field(tmp_path):
    for key in ("root_session_id", "session_id"):
        result = redact({key: RAW_SESSION_ID, "lane_id": "lane_main"}, directory=tmp_path)

        assert key not in result
        assert RAW_SESSION_ID not in repr(result)
        assert FROZEN_HASH_PATTERN.fullmatch(result["root_session_hash"])
        assert result["root_session_hash"] == session_digest(
            RAW_SESSION_ID, directory=tmp_path
        )


def test_session_id_is_dropped_when_the_salt_is_unavailable(tmp_path):
    """The no-silent-fallback rule, on the path where it matters most.

    ``redact`` must not raise, and it must not emit an unsalted digest. The
    only remaining option is to drop the identifier, which is what it does.
    """
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    unusable = blocker / "state"

    result = redact({"root_session_id": RAW_SESSION_ID, "lane_id": "l"}, directory=unusable)

    assert RAW_SESSION_ID not in repr(result)
    assert "root_session_hash" not in result
    assert "root_session_id" in result["_redacted"]
    assert result["lane_id"] == "l"


# ---------------------------------------------------------------------------
# redact() never raises
# ---------------------------------------------------------------------------


class _ExplodingRepr:
    def __repr__(self) -> str:
        raise RuntimeError("this repr is broken")


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(None, id="none"),
        pytest.param("a bare string", id="str"),
        pytest.param(42, id="int"),
        pytest.param([1, 2, 3], id="list"),
        pytest.param(b"\x00\xff raw bytes", id="bytes"),
        pytest.param({"blob": b"\x00\xff"}, id="bytes-value"),
        pytest.param({"obj": object()}, id="arbitrary-object"),
        pytest.param({"bad": _ExplodingRepr()}, id="broken-repr"),
        pytest.param({1: "int key", (2, 3): "tuple key"}, id="non-string-keys"),
        pytest.param(
            {"deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}}, id="deep"
        ),
    ],
)
def test_redact_never_raises(event, tmp_path):
    redact(event, directory=tmp_path)


def test_redact_survives_a_self_referential_structure(tmp_path):
    """A cycle must terminate. A ``RecursionError`` escaping a logging call is
    an outage triggered by the act of trying to observe one."""
    event: dict = {"model": "m"}
    event["self"] = event
    nested: list = ["x"]
    nested.append(nested)
    event["loop"] = nested

    result = redact(event, directory=tmp_path)

    assert result["model"] == "m"


def test_unknown_types_become_truncated_reprs(tmp_path):
    result = redact({"blob": b"A" * 500}, directory=tmp_path)

    assert isinstance(result["blob"], str)
    assert len(result["blob"]) <= 64


def test_deep_nesting_is_truncated_not_recursed_forever(tmp_path):
    node: dict = {"end": True}
    for _ in range(60):
        node = {"next": node}

    result = redact(node, directory=tmp_path)

    assert "truncated" in repr(result)


# ---------------------------------------------------------------------------
# The logging sink
# ---------------------------------------------------------------------------


#: A realistic gateway log event: the ``_hermes_auto`` envelope the provider
#: injects, alongside the request body the gateway relays.
REALISTIC_EVENT = {
    "event": "request_received",
    "_hermes_auto": {
        "protocol_version": 1,
        "root_session_id": RAW_SESSION_ID,
        "virtual_model": "auto:balanced",
        "plugin_version": "0.1.0",
    },
    "model": "auto:balanced",
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "TOP-SECRET-PROMPT-CANARY"},
    ],
    "authorization": "Bearer sk-live-CANARY-TOKEN",
}


def test_formatted_record_carries_the_digest_and_not_the_raw_session_id(tmp_path):
    formatter = RedactingFormatter(directory=tmp_path)
    record = logging.LogRecord(
        name="hermes_auto.gateway",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=REALISTIC_EVENT,
        args=None,
        exc_info=None,
    )

    output = formatter.format(record)

    assert RAW_SESSION_ID not in output
    assert "TOP-SECRET-PROMPT-CANARY" not in output
    assert "sk-live-CANARY-TOKEN" not in output
    assert session_digest(RAW_SESSION_ID, directory=tmp_path) in output
    assert "auto:balanced" in output


def test_get_logger_redacts_end_to_end(tmp_path, capsys):
    logger = get_logger("hermes_auto.test.redaction.e2e", directory=tmp_path)
    logger.setLevel(logging.INFO)

    logger.info(REALISTIC_EVENT)

    captured = capsys.readouterr()
    stream = captured.err + captured.out

    assert RAW_SESSION_ID not in stream
    assert "TOP-SECRET-PROMPT-CANARY" not in stream
    assert "sk-live-CANARY-TOKEN" not in stream
    assert session_digest(RAW_SESSION_ID, directory=tmp_path) in stream


def test_get_logger_does_not_propagate_to_the_root_logger(tmp_path):
    """A propagating logger hands the raw record to root's handlers.

    The redaction would then apply to this logger's own output and to nothing
    else -- the leak lands where a reader is least likely to look.
    """
    logger = get_logger("hermes_auto.test.redaction.propagate", directory=tmp_path)

    assert logger.propagate is False


def test_get_logger_does_not_stack_duplicate_handlers(tmp_path):
    name = "hermes_auto.test.redaction.duplicates"
    first = get_logger(name, directory=tmp_path)
    second = get_logger(name, directory=tmp_path)

    assert first is second
    redacting = [
        h for h in first.handlers if isinstance(h.formatter, RedactingFormatter)
    ]
    assert len(redacting) == 1


def test_the_module_exports_the_documented_interface():
    """The five names plan 02-02 froze, so a later rename is a visible break."""
    for name in ("install_salt", "session_digest", "redact", "get_logger", "RedactionError"):
        assert hasattr(redaction, name), name
        assert name in redaction.__all__, name


def test_default_state_directory_is_resolved_with_stdlib_only(monkeypatch, tmp_path):
    """No import of ``state/paths.py``: it lands in the same wave.

    Plan 02-04 injects ``paths.state_dir()`` when it composes the app. Until
    then the default must resolve on its own, or this module fails to import
    whenever it happens to run before its sibling.
    """
    source = inspect.getsource(redaction)
    assert "from hermes_auto.state" not in source
    assert "import hermes_auto.state" not in source

    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(tmp_path / "from-env"))
    assert redaction._default_state_dir() == tmp_path / "from-env"

    monkeypatch.delenv("HERMES_AUTO_STATE_DIR", raising=False)
    assert redaction._default_state_dir() == pathlib.Path.home() / ".hermes" / "auto-router"
