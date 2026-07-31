"""The on-disk runtime file: the primitive that makes supervision safe.

This file ships to users' machines and every later plan reads it, so its shape is
frozen here and changing it later costs a migration. Three properties are the
reason this module exists, and each one prevents a specific, observed failure.

**1. The write is atomic.** Serialize to a temp file in the same directory,
``flush``, ``os.fsync``, then ``os.replace`` onto the final path. A plain
``open(path, "w")`` truncates first and fills in afterwards, so a concurrent
``status`` -- which runs on a timer and on every session start -- can read a
zero-length or half-written file and conclude the gateway is corrupt when it is
merely starting. ``os.replace`` is atomic on POSIX *and* on Windows, which is
what lets one implementation serve both. The temp file must live in the same
directory as the target, because ``os.replace`` across filesystems is not atomic.

**2. ``instance_id`` is the identity, not the PID.** It is a fresh
``secrets.token_hex(16)`` per process start. Supervision never trusts the
recorded PID: ``status`` and ``doctor`` issue ``GET /healthz`` against the
recorded port and compare the ``instance_id`` in the response against the one in
this file. Match means running; connection refused means stale, remove the file;
a mismatch means some *other* process now owns that port and must be reported
rather than adopted. Windows recycles PIDs quickly, so a stale file plus a
recycled PID is not a rare race -- it is the normal case after an unclean
shutdown, and a PID-based check would report an unrelated process as a healthy
gateway. The next reader of this module will assume the PID is the identity; it
is not, and that is why this paragraph is here.

**3. Absent and corrupt are different answers.** ``read_runtime`` returns
``None`` when the file does not exist -- "not started", the ordinary state on a
clean machine -- and raises ``RuntimeFileError`` when the file exists but does
not parse or is missing a field. Collapsing the two into ``None`` would make
``status`` cheerfully report "stopped" for an install that is actually damaged,
which is the single most misleading thing a status command can do.

**Scope: storage only.** There is deliberately no liveness probing here -- no
signal-zero check, no process table scan, no ``psutil``. Liveness is a
``/healthz`` question answered by plan 02-07's supervisor. Keeping it out is what
lets this module stay standard-library-only, which in turn lets the Hermes-side
provider shim import it across the dependency isolation boundary.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import secrets
import tempfile
from typing import Any

from .paths import runtime_dir

# The supervision contract's filename. Fixed, because the control plugin running
# inside the Hermes environment locates it by name.
RUNTIME_FILE_NAME: str = "gateway.json"

# Bytes of randomness behind instance_id. 16 bytes -> 32 hex characters; the
# collision probability across two starts is what makes the PID-reuse defense
# sound, so this is not a knob worth shrinking.
INSTANCE_ID_BYTES: int = 16

_REQUIRED_INT_FIELDS = ("pid", "port", "admin_port")
_REQUIRED_STR_FIELDS = ("instance_id", "started_at", "exe")


class RuntimeFileError(Exception):
    """The runtime file exists but cannot be trusted.

    Deliberately distinct from "no runtime file", which is not an error and is
    reported by returning ``None``.
    """


@dataclasses.dataclass(frozen=True)
class RuntimeFile:
    """What a running gateway records about itself.

    ``admin_port`` is part of the contract because one process binds both
    listeners and writes this file only after *both* binds succeed
    (02-CONTEXT § Supervision Contract). ``stop`` POSTs to
    ``/admin/v1/shutdown`` as its primary path on every platform -- it is primary
    precisely because Windows has no ``SIGTERM`` -- so a supervisor that could not
    find the admin port would silently degrade every stop into a hard kill and
    cut in-flight streams.
    """

    pid: int
    port: int
    admin_port: int
    instance_id: str
    started_at: str
    exe: str

    def to_json(self) -> str:
        """Serialize deterministically.

        ``sort_keys`` and a fixed indent mean two writes of equal content produce
        byte-identical files, so a diff of this file is always a real change.
        """
        return json.dumps(dataclasses.asdict(self), sort_keys=True, indent=2) + "\n"


def new_instance_id() -> str:
    """Mint a fresh per-process-start identity.

    Called once at startup. Never derived from the PID, the port, or the clock:
    every one of those repeats, and a repeatable identity cannot distinguish this
    process from the dead one whose PID it inherited.
    """
    return secrets.token_hex(INSTANCE_ID_BYTES)


def runtime_path(
    configured: str | os.PathLike[str] | None = None,
    *,
    create: bool = True,
) -> pathlib.Path:
    """Resolve ``<state_dir>/runtime/gateway.json``."""
    return runtime_dir(configured, create=create) / RUNTIME_FILE_NAME


def write_runtime(
    rf: RuntimeFile,
    configured: str | os.PathLike[str] | None = None,
) -> pathlib.Path:
    """Atomically publish the runtime file. Call only after the ports are bound.

    Ordering is part of the contract: writing this file before the listeners are
    up advertises a gateway that cannot answer, and every consumer treats the
    file's existence as "there should be something on that port".
    """
    if not isinstance(rf, RuntimeFile):
        raise RuntimeFileError(
            f"write_runtime expects a RuntimeFile, got {type(rf).__name__}"
        )

    target = runtime_path(configured, create=True)
    payload = rf.to_json().encode("utf-8")

    # Same directory as the target: os.replace is only atomic within a
    # filesystem, and the state dir may well be on a different one than /tmp.
    handle, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{RUNTIME_FILE_NAME}.", suffix=".tmp"
    )
    temp_path = pathlib.Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            # Without fsync the rename can land before the data does, leaving a
            # correctly-named, empty file after a power loss.
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    except OSError as exc:
        # Never leave a stray temp file behind: the directory is scanned by
        # doctor, and litter there reads as a failed install.
        temp_path.unlink(missing_ok=True)
        raise RuntimeFileError(f"{target}: could not be written: {exc}") from exc
    return target


def read_runtime(
    configured: str | os.PathLike[str] | None = None,
) -> RuntimeFile | None:
    """Return the recorded runtime state, or ``None`` if the gateway is not started.

    Raises ``RuntimeFileError`` when the file is present but unusable. See the
    module docstring for why those two outcomes must not be merged.
    """
    target = runtime_path(configured, create=False)

    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        # Present but unreadable -- a permissions problem or a bad mount -- is a
        # broken install, not an absent one.
        raise RuntimeFileError(f"{target}: exists but cannot be read: {exc}") from exc

    try:
        document: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFileError(
            f"{target}: is not valid JSON ({exc}). The gateway may have been "
            "interrupted mid-start; remove the file to clear it."
        ) from exc

    if not isinstance(document, dict):
        raise RuntimeFileError(
            f"{target}: expected a JSON object, got {type(document).__name__}"
        )

    for field in _REQUIRED_INT_FIELDS:
        if field not in document:
            raise RuntimeFileError(f"{target}: missing required field {field!r}")
        value = document[field]
        # bool is a subclass of int; a JSON `true` here means the writer was wrong.
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeFileError(
                f"{target}: field {field!r} must be an integer, got "
                f"{type(value).__name__}"
            )

    for field in _REQUIRED_STR_FIELDS:
        if field not in document:
            raise RuntimeFileError(f"{target}: missing required field {field!r}")
        if not isinstance(document[field], str):
            raise RuntimeFileError(
                f"{target}: field {field!r} must be a string, got "
                f"{type(document[field]).__name__}"
            )

    if not document["instance_id"].strip():
        raise RuntimeFileError(
            f"{target}: 'instance_id' is empty. It is the identity supervision "
            "compares against /healthz, so an empty value would make every "
            "running gateway look like a stale one."
        )

    return RuntimeFile(
        pid=document["pid"],
        port=document["port"],
        admin_port=document["admin_port"],
        instance_id=document["instance_id"],
        started_at=document["started_at"],
        exe=document["exe"],
    )


def clear_runtime(configured: str | os.PathLike[str] | None = None) -> bool:
    """Remove the runtime file. Idempotent; returns whether a file was removed.

    Idempotence matters because this runs on both the clean-shutdown path and the
    stale-file-cleanup path, and those legitimately race.
    """
    target = runtime_path(configured, create=False)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeFileError(f"{target}: could not be removed: {exc}") from exc
    return True


__all__ = [
    "RUNTIME_FILE_NAME",
    "INSTANCE_ID_BYTES",
    "RuntimeFileError",
    "RuntimeFile",
    "new_instance_id",
    "runtime_path",
    "write_runtime",
    "read_runtime",
    "clear_runtime",
]
