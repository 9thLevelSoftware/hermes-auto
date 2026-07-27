"""The single logging sink. Nothing in this project logs an event dict directly.

``docs/privacy.md`` makes four prohibitions -- no raw prompt text, no tool-result
bodies, no secrets, and session identifiers hashed with a local salt -- and then
says plainly that **Phase 1 provides no structural backstop for them**, assigning
the salting to ``telemetry/events.py`` in Phase 8. That is one phase too late:
the gateway receives ``root_session_id`` inside the ``_hermes_auto`` envelope on
the very first request it serves, so Phases 2 through 7 would each write leaking
logs that then need auditing. This module is that backstop, brought forward.

Two design choices carry the weight:

**The salt directory is an injectable parameter with a stdlib-only default.**
This module deliberately does *not* import ``hermes_auto.state.paths``. That
module's ``state_dir()`` is created by plan 02-01 in the same wave, and a
same-wave import would make this module fail to import whenever it happened to
land first. Plan 02-04 passes ``paths.state_dir()`` in as ``directory`` when it
composes the app; until then the default resolves ``HERMES_AUTO_STATE_DIR`` or
``~/.hermes/auto-router`` with nothing but ``os`` and ``pathlib``. The two
resolutions are intended to agree, and 02-04 wiring the real one in is what
makes them agree by construction rather than by coincidence.

**There is no unsalted fallback anywhere in this file.** An unsalted
``sha256(session_id)`` is also sixty-four lowercase hex characters and satisfies
the ``^[0-9a-f]{64}$`` pattern frozen on ``root_session_hash`` identically, so a
silent fallback would be invisible in the data and would be stable across every
machine running the same session -- the exact property the salt exists to
destroy. When the salt is unavailable, :func:`session_digest` raises
:class:`RedactionError` and :func:`redact` **drops** the identifier rather than
emitting anything in its place.

Scope, stated honestly: this sink redacts *structured* records. It matches
banned keys by exact name, case-folded -- not by substring, because
``token_count``, ``prompt_tokens`` and ``completion_tokens`` are precisely the
derived counts ``docs/privacy.md`` says the router legitimately keeps, and a
substring rule on ``token`` or ``prompt`` would delete them. A key named
``user_prompt`` is therefore not matched, and a caller who formats a session id
into a message *string* defeats this module entirely. What is bounded here is
the shape of what a structured record can carry; keeping content out of free
text remains a property of the code that builds the record.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import pathlib
import secrets
import subprocess
import sys
import threading

# Windows allocates a fresh console window for a console-subsystem child when the
# parent has no console of its own. CREATE_NO_WINDOW suppresses it; it is absent on
# POSIX, hence getattr.
_NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

__all__ = [
    "BANNED_KEYS",
    "RedactionError",
    "SALT_FILENAME",
    "SESSION_ID_KEYS",
    "SESSION_HASH_KEY",
    "get_logger",
    "install_salt",
    "redact",
    "session_digest",
]


class RedactionError(Exception):
    """The per-install salt could not be created, read, or trusted.

    Raised instead of returning an unsalted digest. Callers can catch this one
    type rather than ``OSError``, ``ValueError``, and ``subprocess`` failures
    separately -- the same absent-versus-corrupt discipline the rest of the
    project uses, with one deliberate difference: a *missing* salt file is not
    an error (it is created), while an *unreadable or malformed* one is.
    """


#: File name under the state directory. Sits beside the bearer token, which
#: plan 02-01 writes to ``<state_dir>/token`` with the same treatment.
SALT_FILENAME = "salt"

#: Bytes of entropy in a freshly generated salt.
_SALT_BYTES = 32

#: Keys dropped from any event, at any nesting depth. Matched by exact name
#: after case-folding. Plans 02-04 and 02-07 must not log anything on this list
#: and must not assume a near-miss spelling is covered -- it is not.
BANNED_KEYS = frozenset(
    {
        "messages",
        "content",
        "prompt",
        "tool_calls",
        "tool_result",
        "tool_output",
        "arguments",
        "authorization",
        "api_key",
        "token",
        "secret",
    }
)

#: Keys whose value is a raw session identifier. Replaced by
#: :data:`SESSION_HASH_KEY` carrying the salted digest, never passed through.
SESSION_ID_KEYS = frozenset({"root_session_id", "session_id"})

#: The field name Phase 1 froze in both routing schemas, constrained there to
#: ``^[0-9a-f]{64}$``. :func:`session_digest` produces exactly that shape.
SESSION_HASH_KEY = "root_session_hash"

#: Key listing what was dropped at a given nesting level.
_REDACTED_KEY = "_redacted"

#: Recursion ceiling. A logging path must terminate on adversarial input, and a
#: ``RecursionError`` escaping here would turn an observability call into the
#: outage this module is written to avoid.
_MAX_DEPTH = 12

#: Cap applied to the ``repr()`` of a value whose type this module does not
#: recognize -- bytes, datetimes, exceptions, arbitrary objects.
_REPR_LIMIT = 64

#: Scalars that pass through untouched.
_PASSTHROUGH_TYPES = (str, bool, int, float, type(None))

#: Resolved-directory -> salt bytes. Reading the salt file on every request
#: would put a syscall on the hot path; the salt never changes once created.
_salt_cache: dict[pathlib.Path, bytes] = {}
_salt_lock = threading.Lock()


def _default_state_dir() -> pathlib.Path:
    """Resolve the state directory using stdlib only.

    Mirrors what ``hermes_auto.state.paths.state_dir()`` resolves, without
    importing it -- see this module's docstring for why that import is deferred
    to plan 02-04's composition step rather than taken here.
    """
    override = os.environ.get("HERMES_AUTO_STATE_DIR")
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path.home() / ".hermes" / "auto-router"


def _restrict_permissions(path: pathlib.Path) -> None:
    """Apply the same permission treatment the bearer token gets.

    On POSIX the mode is set at creation time by :func:`os.open`; this call is
    a no-op reassertion there. On Windows ``os.chmod`` moves only the read-only
    bit and leaves inherited ACLs intact, so a ``0o600`` call silently leaves
    the file readable by every other account on the machine. ``icacls`` is the
    only thing that actually narrows it.

    Failure raises rather than warning. A salt whose permissions could not be
    narrowed is the failure mode this module exists to prevent, and there is no
    degraded mode to fall back to that is not simply "no protection".
    """
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return

    account = os.environ.get("USERNAME") or os.environ.get("USER")
    if not account:
        raise RedactionError(
            f"{path}: cannot restrict permissions -- neither USERNAME nor USER "
            f"is set, so there is no account to grant. Refusing to leave the "
            f"salt with inherited ACLs."
        )

    try:
        completed = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{account}:F",
            ],
            capture_output=True,
            text=True,
            check=False,
            creationflags=_NO_CONSOLE_WINDOW,
        )
    except OSError as exc:  # icacls absent or not executable
        raise RedactionError(f"{path}: could not run icacls: {exc}") from exc

    if completed.returncode != 0:
        raise RedactionError(
            f"{path}: icacls failed with exit {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )


def _read_salt_file(path: pathlib.Path) -> bytes:
    """Return the salt stored at *path*.

    Absent is handled by the caller; here, present-but-malformed is a typed
    error. Coercing a truncated or garbled salt into "some bytes" would produce
    digests that are stable and wrong, which is worse than a loud failure.
    """
    try:
        text = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RedactionError(f"{path}: salt file could not be read: {exc}") from exc

    try:
        salt = bytes.fromhex(text)
    except ValueError as exc:
        raise RedactionError(
            f"{path}: salt file is not hex-encoded; refusing to guess at its "
            f"contents. Delete it to have a new salt generated -- note that "
            f"digests written under the old salt will no longer correlate."
        ) from exc

    if len(salt) != _SALT_BYTES:
        raise RedactionError(
            f"{path}: salt is {len(salt)} bytes, expected {_SALT_BYTES}"
        )
    return salt


def install_salt(directory: pathlib.Path | None = None) -> str:
    """Return the per-install salt as lowercase hex, creating it if absent.

    Idempotent: the first call generates ``secrets.token_bytes(32)`` and writes
    it; every later call returns the same value. The hex encoding is what is
    both stored and returned, so the file is inspectable with ``cat`` and the
    return type is a plain ``str``.

    Args:
        directory: State directory to hold the salt file. Defaults to
            ``HERMES_AUTO_STATE_DIR`` or ``~/.hermes/auto-router``. Injected
            rather than imported so this module does not depend on
            ``state/paths.py``, which lands in the same wave.

    Returns:
        The salt, hex-encoded -- 64 lowercase hex characters.

    Raises:
        RedactionError: The directory or file could not be created, its
            permissions could not be restricted, or an existing salt file is
            unreadable or malformed. Never returns a fallback value.
    """
    return _salt_for(directory).hex()


def _salt_for(directory: pathlib.Path | None) -> bytes:
    """Return the salt bytes for *directory*, caching per resolved path.

    Caching is keyed by directory rather than held in one global so that two
    different state directories yield two different salts within one process.
    That is what makes the divergence property testable without a test-only
    reset hook poking at module state.
    """
    state_dir = (
        _default_state_dir() if directory is None else pathlib.Path(directory)
    ).expanduser()

    try:
        state_dir = state_dir.resolve()
    except OSError as exc:
        raise RedactionError(f"{state_dir}: state directory is not usable: {exc}") from exc

    with _salt_lock:
        cached = _salt_cache.get(state_dir)
        if cached is not None:
            return cached

        salt = _load_or_create_salt(state_dir)
        _salt_cache[state_dir] = salt
        return salt


def _load_or_create_salt(state_dir: pathlib.Path) -> bytes:
    """Read the salt at ``<state_dir>/salt``, generating it on first use."""
    path = state_dir / SALT_FILENAME

    if path.exists():
        return _read_salt_file(path)

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RedactionError(
            f"{state_dir}: state directory could not be created: {exc}"
        ) from exc

    salt = secrets.token_bytes(_SALT_BYTES)

    # O_EXCL rather than a plain open: if a second process created the salt
    # between the exists() check above and here, the loser of that race must
    # adopt the winner's salt. Writing over it would give the two processes
    # different digests for the same session, which is the one corruption mode
    # that would be invisible in the output.
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _read_salt_file(path)
    except OSError as exc:
        raise RedactionError(f"{path}: salt file could not be created: {exc}") from exc

    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(salt.hex())
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RedactionError(f"{path}: salt file could not be written: {exc}") from exc

    try:
        _restrict_permissions(path)
    except RedactionError:
        # A salt that exists but is world-readable is worse than no salt file,
        # because the next call would read it back and trust it. Remove it so
        # the failure stays loud instead of curing itself on retry.
        try:
            path.unlink()
        except OSError:
            pass
        raise

    return salt


def session_digest(
    session_id: str,
    *,
    directory: pathlib.Path | None = None,
) -> str:
    """Return the salted digest of *session_id* as 64 lowercase hex characters.

    HMAC-SHA256 rather than ``sha256(salt + session_id)``: the concatenated
    form invites a length-extension question that a reviewer then has to reason
    about, and HMAC removes the question rather than answering it.

    The output matches ``^[0-9a-f]{64}$``, the pattern Phase 1 froze on
    ``root_session_hash`` in both ``outcome-event.v1`` and
    ``route-decision.v1``.

    Args:
        session_id: The raw identifier. Never logged, never returned.
        directory: State directory holding the salt. See :func:`install_salt`.

    Raises:
        RedactionError: The salt is unavailable. There is no unsalted path.
        TypeError: *session_id* is not a ``str``. This function is a typed API,
            not the never-raising logging path -- that is :func:`redact`.
    """
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be str, got {type(session_id).__name__}")

    salt = _salt_for(directory)
    return hmac.new(salt, session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def redact(event: object, *, directory: pathlib.Path | None = None) -> object:
    """Return a redacted copy of *event*. Never raises.

    Banned keys are **dropped** and named in a ``_redacted`` list at the level
    they were dropped from, so a reader can see that something was removed and
    what it was without seeing any of it. Keys holding a raw session id become
    ``root_session_hash`` carrying the salted digest.

    This function does not raise. A logging call that raises converts an
    observability path into an outage, and the first place that would bite is a
    request handler's error branch -- the moment logging matters most. Every
    failure therefore degrades to dropping the affected value:

    * If the salt is unavailable, a session id is **dropped**, not passed
      through and not hashed unsalted. The identifier is recorded as redacted
      like any other banned key.
    * A structure deeper than 12 levels, or one that refers to itself, yields a
      placeholder string at the point the limit is hit.
    * A value of a type this module does not recognize becomes its ``repr()``
      truncated to 64 characters.

    Idempotent: ``redact(redact(e)) == redact(e)``. The ``_redacted`` list a
    first pass adds contains only plain strings, none of which are banned keys,
    so a second pass carries it through unchanged.

    Args:
        event: Usually a dict. Any object is accepted; a non-container is
            returned in its redacted scalar form.
        directory: State directory holding the salt. See :func:`install_salt`.

    Returns:
        A new object. The input is never mutated.
    """
    try:
        return _redact_node(event, directory, depth=0, seen=frozenset())
    except Exception as exc:  # noqa: BLE001 -- the whole point is not to escape
        # Unreachable by design; kept because "never raises" must hold even if
        # a future edit to a helper is wrong. Names the type, never the value.
        return {
            _REDACTED_KEY: ["<event>"],
            "_redaction_error": type(exc).__name__,
        }


def _redact_node(
    node: object,
    directory: pathlib.Path | None,
    *,
    depth: int,
    seen: frozenset[int],
) -> object:
    """Recursive worker for :func:`redact`. Must not raise."""
    if depth > _MAX_DEPTH:
        return f"<truncated: depth > {_MAX_DEPTH}>"

    if isinstance(node, dict):
        if id(node) in seen:
            return "<cycle>"
        return _redact_mapping(node, directory, depth=depth, seen=seen | {id(node)})

    if isinstance(node, (list, tuple)):
        if id(node) in seen:
            return "<cycle>"
        inner = seen | {id(node)}
        return [
            _redact_node(item, directory, depth=depth + 1, seen=inner) for item in node
        ]

    if isinstance(node, _PASSTHROUGH_TYPES):
        return node

    try:
        rendered = repr(node)
    except Exception:  # noqa: BLE001 -- a __repr__ may itself be broken
        return f"<unreprable {type(node).__name__}>"
    return rendered[:_REPR_LIMIT]


def _redact_mapping(
    node: dict,
    directory: pathlib.Path | None,
    *,
    depth: int,
    seen: frozenset[int],
) -> dict:
    """Redact one mapping level, recording drops local to that level."""
    result: dict = {}
    dropped: list[str] = []

    for key, value in node.items():
        name = key if isinstance(key, str) else repr(key)[:_REPR_LIMIT]
        folded = name.casefold()

        if folded in BANNED_KEYS:
            dropped.append(name)
            continue

        if folded in SESSION_ID_KEYS:
            digest = _safe_digest(value, directory)
            if digest is None:
                # No salt, or an id that is not a string. Either way the raw
                # value does not survive and nothing stands in for it.
                dropped.append(name)
            else:
                result[SESSION_HASH_KEY] = digest
            continue

        result[name] = _redact_node(value, directory, depth=depth + 1, seen=seen)

    if dropped:
        existing = node.get(_REDACTED_KEY)
        prior = [x for x in existing if isinstance(x, str)] if isinstance(existing, list) else []
        merged: list[str] = []
        for entry in [*prior, *dropped]:
            if entry not in merged:
                merged.append(entry)
        result[_REDACTED_KEY] = merged

    return result


def _safe_digest(value: object, directory: pathlib.Path | None) -> str | None:
    """Digest *value*, or return ``None`` so the caller drops the key.

    ``None`` is the only failure signal. Returning the raw id would defeat the
    module, and returning an unsalted digest would defeat it invisibly.
    """
    if not isinstance(value, str):
        return None
    try:
        return session_digest(value, directory=directory)
    except (RedactionError, TypeError):
        return None


class RedactingFormatter(logging.Formatter):
    """Formatter that runs every structured record through :func:`redact`.

    Placing redaction in the formatter rather than asking each call site to
    remember it is the whole point: the sink is a boundary, not a convention.
    The record is copied before its payload is replaced, because handlers share
    records and mutating one would leak the redaction into a sibling handler's
    view -- or, worse, leave the raw payload visible to whichever handler ran
    first.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        directory: pathlib.Path | None = None,
    ) -> None:
        super().__init__(fmt or "%(levelname)s %(name)s %(message)s", datefmt)
        self._directory = directory

    def format(self, record: logging.LogRecord) -> str:
        safe = logging.makeLogRecord(record.__dict__)
        safe.msg = redact(record.msg, directory=self._directory)

        if isinstance(record.args, dict):
            safe.args = redact(record.args, directory=self._directory)
        elif isinstance(record.args, tuple):
            safe.args = tuple(
                redact(item, directory=self._directory) for item in record.args
            )

        return super().format(safe)


def get_logger(
    name: str,
    *,
    directory: pathlib.Path | None = None,
) -> logging.Logger:
    """Return a logger whose every record passes through :func:`redact`.

    ``propagate`` is set to ``False`` deliberately. A propagating logger hands
    the *unredacted* record to the root logger's handlers, which have their own
    formatters, so the redaction would apply to this logger's output and to
    nothing else -- the leak would sit exactly where a reader is least likely
    to look for it.

    Repeat calls with the same name reuse the handler already attached rather
    than stacking duplicates.
    """
    logger = logging.getLogger(name)
    logger.propagate = False

    if not any(
        isinstance(handler.formatter, RedactingFormatter)
        for handler in logger.handlers
    ):
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingFormatter(directory=directory))
        logger.addHandler(handler)

    return logger
