"""Cross-platform sidecar supervision keyed on identity, never on a PID.

This module answers one question that is much harder than it looks: *is the
process I recorded still the process that is running?* Every other function here
is downstream of that answer.

**Liveness is ``instance_id``, not the PID.** ``<state_dir>/runtime/gateway.json``
records a PID, and this module never trusts it for a liveness decision. It issues
``GET /healthz`` on the recorded port and compares the ``instance_id`` in the
response against the one in the file. ``/healthz`` deliberately reports the
*answering process's own* id rather than re-reading the file (plan 02-04,
``gateway/app.py``), which is what makes the mismatch branch reachable at all: if
both sides read the file they would agree by construction and the check would be
decorative. Windows recycles PIDs within seconds, so a stale runtime file plus a
recycled PID is not an exotic race -- it is the ordinary state of a machine after
an unclean shutdown, and a PID-based check reports an unrelated process as a
healthy gateway. This check needs no third-party process-table library, and
behaves identically on Windows, macOS and Linux.

**"Connection refused" is not trustworthy evidence, so a bind decides.** The
textbook stale-file test is "the port refuses connections". On this project's
development machine a connect to a port that has *never been used* times out
after the full timeout instead of being refused -- a local security product
drops the SYN rather than answering it -- so that branch never fires and no
stale runtime file would ever be cleaned. When ``/healthz`` does not answer,
supervision therefore asks the kernel whether the address can be **bound**,
which is a local question no firewall can distort. A timeout with the port
still held is reported as *unresponsive* and the file is left alone, because a
loaded gateway looks exactly like that and removing its file would strand a
live process. See :func:`_port_binding`.

**Every probe follows ``gateway.url``, and every address it resolves to.** The
gateway binds the first ``getaddrinfo`` result for the configured host, and
``require_loopback`` permits ``localhost`` and ``::1`` as well as ``127.0.0.1``.
Probing a hardcoded ``127.0.0.1`` against a gateway on ``::1`` found the IPv4
address free, declared a serving process stale, deleted its runtime file and
left it unreapable. So the probe host comes from configuration
(:func:`probe_host`) and the bind test must find *all* of its addresses free
before it will call a port free (:func:`_port_binding`).

**Stopping goes through the admin API first, on every platform.** Windows has no
``SIGTERM``: ``os.kill`` there calls ``TerminateProcess``, which is the hard kill,
not a request. So the graceful rung is ``POST /admin/v1/shutdown``, which sets
uvicorn's ``should_exit`` and lets in-flight streaming completions finish. The
escalation below it is genuinely shorter on Windows than on POSIX, and that
asymmetry is the reason the admin listener exists at all
(02-CONTEXT § Supervision Contract).

**Killing by PID is permitted only after identity has just been confirmed.**
``stop`` re-probes ``/healthz`` and requires the recorded ``instance_id`` to still
match immediately before it signals anything. That does not *close* the race --
the process could exit and its PID be reused in the microseconds after the probe
returns -- but it narrows it from "the whole time since the file was written" to
"one round trip on loopback", and it means a stale file alone can never cause a
signal to be sent. The residual window is stated rather than claimed closed, the
same way ``gateway/auth.py`` states its ``icacls`` window.

**Output goes to a file, never a pipe.** A detached sidecar has no parent draining
its output; a full OS buffer would block the writing process forever, and the
symptom is a gateway that serves a few hundred requests and then hangs with no
error anywhere. The log is size-rotated at spawn time.

**Spawn is detached.** POSIX gets ``start_new_session=True``; Windows gets
``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS``, retried with
``CREATE_BREAKAWAY_FROM_JOB`` when the parent sits inside a job object -- common
on CI runners, where an otherwise-detached child is killed with the parent. The
retry is ordered that way round because ``CREATE_BREAKAWAY_FROM_JOB`` *fails*
with access denied when the job does not permit breakaway, so it cannot be the
first attempt.

Scope: start on demand only. No systemd unit, launchd plist, or Windows Service
-- that is ``design.md`` §11.3 layer 2 and belongs to Phase 11.
"""

from __future__ import annotations

import contextlib
import dataclasses
import ipaddress
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import AutoRouterConfig, ConfigError, load_config
from .state.paths import log_dir as resolve_log_dir
from .state.runtime import RuntimeFile, RuntimeFileError, clear_runtime, read_runtime
from .telemetry.redaction import get_logger

__all__ = [
    "DEFAULT_STOP_TIMEOUT_SECONDS",
    "GATEWAY_MODULE",
    "LOG_FILE_NAME",
    "STATUS_CORRUPT",
    "STATUS_FOREIGN",
    "STATUS_NOT_STARTED",
    "STATUS_RUNNING",
    "STATUS_STALE_CLEANED",
    "STATUS_UNRESPONSIVE",
    "Status",
    "SupervisorError",
    "authority",
    "gateway_log_path",
    "probe_host",
    "restart",
    "start",
    "status",
    "stop",
]

#: The module the sidecar runs. ``python -m hermes_auto.gateway.main``.
GATEWAY_MODULE = "hermes_auto.gateway.main"

#: The loopback address probes fall back to when ``gateway.url`` does not name a
#: usable one. **Not** the address probes always use: see :func:`probe_host` for
#: why hardcoding it orphaned a live gateway.
#:
#: This constant is a *fallback*, and any caller reaching for it in place of
#: :func:`probe_host` has reintroduced that bug. ``commands.py`` did exactly
#: that, which is why :func:`probe_host` and :func:`authority` are public.
PROBE_HOST = "127.0.0.1"

#: One health probe's ceiling. Short, because ``status`` runs on every session
#: start and a slow status command is a slow shell.
HEALTH_TIMEOUT_SECONDS = 2.0

#: Default ceiling for the whole graceful-stop sequence.
DEFAULT_STOP_TIMEOUT_SECONDS = 10.0

#: How long ``stop`` gives the admin listener to accept the shutdown request
#: itself. Separate from the drain: accepting is fast even when draining is not.
ADMIN_REQUEST_TIMEOUT_SECONDS = 5.0

#: Gap between liveness polls while waiting for a state change.
POLL_INTERVAL_SECONDS = 0.1

LOG_FILE_NAME = "gateway.log"

#: Rotate at spawn when the log has grown past this, keeping this many older
#: generations. Deliberately not ``logging.handlers.RotatingFileHandler``: the
#: writer is a *different process* holding an inherited file descriptor, so
#: rotation can only happen at the moment the descriptor is handed over.
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

#: ``Status.kind`` values. A machine-readable discriminator, because the callers
#: (``commands.py``, the session-start hook) must distinguish "nothing is
#: running" from "somebody else owns your port" to choose an exit code, and
#: pattern-matching ``detail`` prose is precisely the fragile coupling that
#: breaks the first time a message is reworded.
STATUS_RUNNING = "running"
STATUS_NOT_STARTED = "not_started"
STATUS_STALE_CLEANED = "stale_cleaned"
STATUS_FOREIGN = "foreign"
STATUS_UNRESPONSIVE = "unresponsive"
STATUS_CORRUPT = "corrupt"

# Health-probe outcomes, internal.
_PROBE_OK = "ok"
_PROBE_REFUSED = "refused"
_PROBE_TIMEOUT = "timeout"
_PROBE_FOREIGN = "foreign"

# Bind-probe outcomes, internal. ``residue`` is the POSIX ``TIME_WAIT`` case:
# nothing is listening, but a connection closed here recently. See
# :func:`_port_binding`.
_BIND_FREE = "free"
_BIND_RESIDUE = "residue"
_BIND_HELD = "held"


class SupervisorError(Exception):
    """A supervision action could not be completed.

    Not raised for any *state* the gateway may legitimately be in. "Not running"
    is an answer, not an error; so is "already running". This is reserved for
    "you asked me to do something and I could not do it".
    """


@dataclasses.dataclass(frozen=True)
class Status:
    """What supervision can prove about the gateway right now.

    ``running`` is true only when ``/healthz`` answered on the recorded port
    **and** echoed the recorded ``instance_id``. Every other combination is
    false, with ``kind`` naming which one, so no caller has to infer a state
    machine from a sentence.
    """

    running: bool
    instance_id: str | None
    port: int | None
    pid: int | None
    detail: str
    kind: str = STATUS_NOT_STARTED

    def __str__(self) -> str:
        return self.detail


# ---------------------------------------------------------------------------
# Configuration and paths
# ---------------------------------------------------------------------------


def _resolve_config(config: AutoRouterConfig | None) -> AutoRouterConfig:
    if config is not None:
        return config
    try:
        return load_config(None)
    except ConfigError as exc:
        raise SupervisorError(f"configuration is unusable: {exc}") from exc


def probe_host(config: AutoRouterConfig | None) -> str:
    """The loopback host supervision must probe: the one the gateway binds.

    ``None`` yields :data:`PROBE_HOST`. ``commands.run_doctor`` can reach this
    with an unresolved config -- it is documented to run every check and never
    raise for a failure -- and a probe host is a diagnostic input, so falling
    back to the documented default is right where raising would suppress the
    rest of the report.

    This used to be the constant ``127.0.0.1`` and that was a live bug, not a
    simplification. ``gateway/main.py``'s ``require_loopback`` accepts
    ``localhost`` and ``::1`` as well, and ``bind_socket`` binds
    ``getaddrinfo(...)[0]`` -- which for ``localhost`` is ``::1`` first on this
    machine, on macOS, and on any IPv6-enabled Linux. With
    ``gateway.url: http://localhost:8787`` the gateway therefore answered on
    ``::1`` while supervision asked ``127.0.0.1``: ``/healthz`` was refused, the
    *IPv4* address bound freely, and "the bind decides" concluded **stale** with
    total confidence about the wrong address family. The runtime file of a
    process that was serving traffic was deleted, and ``stop`` could no longer
    reap it. Cross-platform, not a Windows quirk.

    ``require_loopback`` is deliberately reimplemented here rather than imported:
    it lives in ``gateway/main.py``, which pulls in uvicorn and Starlette, and
    ``status`` runs on every Hermes session start. Where the two could disagree
    this one is the more conservative -- it never raises, and falls back to
    :data:`PROBE_HOST` for anything it cannot vouch for, because a probe host is
    a diagnostic input and refusing to report a status is worse than reporting
    one against the documented default.
    """
    if config is None:
        return PROBE_HOST
    try:
        host = urllib.parse.urlsplit(config.gateway.url).hostname
    except ValueError:
        return PROBE_HOST
    if not host:
        return PROBE_HOST
    if host == "localhost":
        return host
    try:
        if not ipaddress.ip_address(host).is_loopback:
            return PROBE_HOST
    except ValueError:
        return PROBE_HOST
    return host


def authority(host: str, port: int) -> str:
    """``host:port`` for a URL, bracketing an IPv6 literal as RFC 3986 requires.

    Public because any caller that resolves a probe host with :func:`probe_host`
    must also format it, and ``f"{host}:{port}"`` produces the unparseable
    ``http://::1:8787/`` for the exact address family :func:`probe_host` exists
    to get right.
    """
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def gateway_log_path(config: AutoRouterConfig | None = None) -> pathlib.Path:
    """Where the sidecar's stdout and stderr land."""
    config = _resolve_config(config)
    return resolve_log_dir(config.gateway.state_dir) / LOG_FILE_NAME


def _rotate_log(path: pathlib.Path) -> None:
    """Shift the log generations if the current file has grown past the cap.

    Best effort by design: a rotation that fails must not prevent the gateway
    from starting. Losing log history is an inconvenience; refusing to start the
    router because of it is an outage.
    """
    try:
        if not path.exists() or path.stat().st_size < MAX_LOG_BYTES:
            return
        oldest = path.with_name(f"{path.name}.{LOG_BACKUP_COUNT}")
        with contextlib.suppress(OSError):
            oldest.unlink()
        for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                with contextlib.suppress(OSError):
                    os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
        with contextlib.suppress(OSError):
            os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        return


# ---------------------------------------------------------------------------
# The health probe -- the whole basis of every decision below
# ---------------------------------------------------------------------------


def _probe_health(
    host: str, port: int, *, timeout: float = HEALTH_TIMEOUT_SECONDS
) -> tuple[str, str | None]:
    """``GET /healthz`` on *host*:*port*. Returns ``(outcome, instance_id)``.

    *host* comes from :func:`probe_host` and is the host the gateway was
    configured to bind, not a constant. When it is the name ``localhost`` it is
    passed through as a name on purpose: ``http.client`` walks every
    ``getaddrinfo`` result, so a gateway on ``::1`` is reached even where the
    IPv4 entry sorts first.

    Outcomes are branched finely on purpose:

    ``ok``
        A gateway answered and reported an ``instance_id``. Whether it is *our*
        gateway is the caller's comparison to make.
    ``refused``
        Nothing is listening. This is the only outcome that justifies removing
        the runtime file.
    ``timeout``
        Something is listening and did not answer in time. A loaded gateway does
        this. Treating it as ``refused`` would delete a live gateway's runtime
        file, which is why the two are not merged.
    ``foreign``
        Something answered but is not this gateway -- an HTTP error, a
        non-JSON body, or JSON without an ``instance_id``. Some other service
        owns the port.
    """
    url = f"http://{authority(host, port)}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read(64 * 1024)
    except urllib.error.HTTPError:
        # A live listener that answered with a status code. Not our gateway --
        # /healthz is unauthenticated here precisely so that a 401 means
        # somebody else's service.
        return _PROBE_FOREIGN, None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return _PROBE_TIMEOUT, None
        return _PROBE_REFUSED, None
    except (TimeoutError, socket.timeout):
        return _PROBE_TIMEOUT, None
    except OSError:
        return _PROBE_REFUSED, None

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _PROBE_FOREIGN, None
    if not isinstance(document, dict):
        return _PROBE_FOREIGN, None
    answered = document.get("instance_id")
    if not isinstance(answered, str) or not answered:
        return _PROBE_FOREIGN, None
    return _PROBE_OK, answered


def _bind_one(
    family: int, socktype: int, proto: int, address: object
) -> str:
    """Classify a single resolved address: free, TIME_WAIT residue, or held."""
    sock = socket.socket(family, socktype, proto)
    try:
        sock.bind(address)  # type: ignore[arg-type]
        return _BIND_FREE
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sock.close()

    if os.name == "nt":
        # No SO_REUSEADDR retry here, ever. On Windows it lets a second process
        # bind a port another process is *actively listening on*, so a retry
        # would classify a live gateway as free and strand it -- the same class
        # of bug this function exists to prevent, and the reason `bind_socket`
        # sets the option only on POSIX.
        return _BIND_HELD

    # POSIX only, and only after a plain bind has already failed. Here
    # SO_REUSEADDR permits exactly one thing a plain bind forbids: binding over
    # a socket in TIME_WAIT. It does *not* permit binding over an active
    # listener -- that would be SO_REUSEPORT -- so success below means "nothing
    # is listening; a connection merely closed here recently".
    sock = socket.socket(family, socktype, proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)  # type: ignore[arg-type]
        return _BIND_RESIDUE
    except OSError:
        return _BIND_HELD
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def _port_binding(host: str, port: int) -> str:
    """Who holds *host*:*port*? ``free``, ``residue``, or ``held``.

    This is the decisive stale-file test, and it is decisive precisely because
    it never puts a packet on the wire.

    "Connection refused" is the textbook signal for "nothing is listening", and
    on this project's development machine it is **not reliable**: a connect to a
    port that has never been used times out after the full timeout rather than
    being refused, because a local security product drops the SYN instead of
    answering it. Measured, on a never-used port, a port a gateway had just
    released, and a port carrying TIME_WAIT residue -- all three time out. Under
    that behaviour the ``refused`` branch never fires, every dead gateway looks
    merely slow, and no stale runtime file is ever cleaned. Asking the kernel
    whether the address is free asks the question that was actually meant.

    **Every resolved address is tried, and ``held`` wins.** The previous version
    returned on the first candidate, which for a ``localhost`` gateway meant it
    answered about IPv4 while the listener sat on ``::1`` -- confidently
    declaring a serving process stale. A name that resolves to several addresses
    is free only when *all* of them are.

    **``residue`` exists so a crash does not lock out a restart.** A plain bind
    to a POSIX port carrying ``TIME_WAIT`` fails, and reporting that as ``held``
    made ``status`` say *unresponsive* and ``start`` refuse to start -- for up to
    a minute after a crash, which is the moment ``start`` most needs to work,
    while claiming a process holds a port that no process holds. ``SO_REUSEADDR``
    distinguishes the two cases and is the only thing that can: see
    :func:`_bind_one` for why the retry is safe on POSIX and forbidden on
    Windows. Callers that only care whether a live process is there should use
    :func:`_port_bindable`, which folds ``residue`` in with ``free``; the safe
    direction -- never call a held port free -- is unchanged.

    The bind is held for microseconds and only on the path where the gateway is
    believed dead, so the window in which it could refuse a genuinely restarting
    gateway is narrower than the one ``start`` already tolerates.
    """
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    except OSError:
        return _BIND_HELD
    if not infos:
        return _BIND_HELD

    residue = False
    for family, socktype, proto, _, address in infos:
        outcome = _bind_one(family, socktype, proto, address)
        if outcome == _BIND_HELD:
            return _BIND_HELD
        if outcome == _BIND_RESIDUE:
            residue = True
    return _BIND_RESIDUE if residue else _BIND_FREE


def _port_bindable(host: str, port: int) -> bool:
    """True when no process is listening on *host*:*port*.

    ``TIME_WAIT`` residue counts as bindable: the process that held the socket
    has gone, which is the only question the callers of this wrapper ask.
    """
    return _port_binding(host, port) != _BIND_HELD


def _clear_runtime_if_ours(
    state_dir: pathlib.Path | None, instance_id: str | None
) -> bool:
    """Remove the runtime file only while it still names *instance_id*.

    ``clear_runtime`` unlinks whatever is at the path. Every caller here reached
    its decision from a record read some milliseconds earlier, and in that gap a
    replacement gateway may have bound its ports and published a *new* file.
    Unlinking that one leaves a process serving traffic with nothing on disk
    naming it -- ``status`` then says ``not_started`` and ``stop`` has nothing to
    reap it by, which is exactly the incident this re-check exists to prevent.

    Read-compare-unlink is not atomic; there is no portable compare-and-unlink.
    This narrows the window to a single file read rather than closing it, and is
    stated rather than claimed closed.
    """
    if not instance_id:
        return clear_runtime(state_dir)
    try:
        current = read_runtime(state_dir)
    except RuntimeFileError:
        return False
    if current is None or current.instance_id != instance_id:
        return False
    return clear_runtime(state_dir)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status(config: AutoRouterConfig | None = None) -> Status:
    """Report what is actually running, without trusting the recorded PID.

    Side effect, and the only one: a runtime file whose port refuses connections
    is removed, because leaving it makes every later ``status`` re-derive the
    same conclusion and makes ``start`` believe the port is taken.
    """
    config = _resolve_config(config)
    state_dir = config.gateway.state_dir
    # Resolved once, from configuration, and threaded through every probe below.
    # A hardcoded 127.0.0.1 here orphaned an IPv6-bound gateway: see probe_host.
    host = probe_host(config)

    try:
        record = read_runtime(state_dir)
    except RuntimeFileError as exc:
        # Present but unparseable. Deliberately not removed: something wrote
        # nonsense there and deleting the evidence is how that bug survives.
        return Status(
            running=False,
            instance_id=None,
            port=None,
            pid=None,
            detail=(
                f"the runtime file exists but cannot be read: {exc}. Remove it "
                f"to clear this state."
            ),
            kind=STATUS_CORRUPT,
        )

    if record is None:
        return Status(
            running=False,
            instance_id=None,
            port=None,
            pid=None,
            detail="not running (no runtime file; the gateway has not been started)",
            kind=STATUS_NOT_STARTED,
        )

    outcome, answered = _probe_health(host, record.port)

    if outcome == _PROBE_OK and answered == record.instance_id:
        return Status(
            running=True,
            instance_id=record.instance_id,
            port=record.port,
            pid=record.pid,
            detail=(
                f"running on {authority(host, record.port)} "
                f"(instance {record.instance_id}, pid {record.pid})"
            ),
            kind=STATUS_RUNNING,
        )

    if outcome == _PROBE_OK:
        # A gateway answered, with a different identity. Our recorded process is
        # gone and something else -- very possibly another copy of this project
        # for a different state directory -- holds the port. Report it; killing
        # it would be killing a process this install does not own.
        return Status(
            running=False,
            instance_id=None,
            port=record.port,
            pid=None,
            detail=(
                f"another gateway owns {authority(host, record.port)}: it reports "
                f"instance {answered}, the runtime file records "
                f"{record.instance_id}. Nothing was stopped or removed -- that "
                f"process does not belong to this install."
            ),
            kind=STATUS_FOREIGN,
        )

    if outcome == _PROBE_FOREIGN:
        return Status(
            running=False,
            instance_id=None,
            port=record.port,
            pid=None,
            detail=(
                f"something is listening on {authority(host, record.port)} but it is "
                f"not a hermes-auto gateway (no usable /healthz). Nothing was "
                f"stopped or removed."
            ),
            kind=STATUS_FOREIGN,
        )

    # Refused or timed out. Neither one alone proves the port is free -- see
    # `_port_binding` for the measured reason a timeout is the *normal* answer
    # for a dead port here. The bind decides, across every address the host
    # resolves to.
    binding = _port_binding(host, record.port)
    if binding == _BIND_HELD:
        return Status(
            running=False,
            instance_id=record.instance_id,
            port=record.port,
            pid=record.pid,
            detail=(
                f"a process holds {authority(host, record.port)} but did not "
                f"answer /healthz within {HEALTH_TIMEOUT_SECONDS}s. The runtime "
                f"file was left in place: a loaded gateway looks like this, and "
                f"removing it would strand a live process. On Windows this can "
                f"also be TIME_WAIT residue from a connection that closed "
                f"seconds ago rather than a live listener -- if so it clears on "
                f"its own within about a minute and `start` will work again."
            ),
            kind=STATUS_UNRESPONSIVE,
        )

    # Nothing is listening. The file is stale. Cleared identity-scoped: between
    # the read above and here a replacement gateway may have published its own
    # file, and unlinking *that* one would orphan a process that is serving --
    # the same failure, reached from the other side.
    removed = False
    with contextlib.suppress(RuntimeFileError):
        removed = _clear_runtime_if_ours(state_dir, record.instance_id)
    residue_note = (
        " A connection closed there recently (TIME_WAIT); nothing is listening."
        if binding == _BIND_RESIDUE
        else ""
    )
    return Status(
        running=False,
        instance_id=None,
        port=record.port,
        pid=None,
        detail=(
            f"not running: nothing holds {authority(host, record.port)}. "
            + (
                "The stale runtime file was removed."
                if removed
                else "The stale runtime file was already gone."
            )
            + residue_note
        ),
        kind=STATUS_STALE_CLEANED,
    )


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _spawn(config: AutoRouterConfig) -> tuple[subprocess.Popen[bytes], str]:
    """Launch the sidecar detached with file-backed output.

    Returns the handle and the name of the creation strategy that worked, which
    the caller reports -- on Windows, which of the two flag sets was needed is
    the single most useful fact when a start fails on a CI runner.
    """
    log_path = gateway_log_path(config)
    _rotate_log(log_path)

    command = [sys.executable, "-m", GATEWAY_MODULE]
    # The state directory, so a detached sidecar does not hold a handle on the
    # user's project directory -- on Windows that alone can make the directory
    # undeletable long after the shell that started it has gone.
    working_directory = str(resolve_log_dir(config.gateway.state_dir).parent)

    attempts: list[tuple[str, dict[str, object]]] = []
    if os.name == "nt":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        attempts.append(
            ("detached+new_process_group", {"creationflags": detached | new_group})
        )
        # Only as a retry: CREATE_BREAKAWAY_FROM_JOB fails outright with access
        # denied when the containing job forbids breakaway, so leading with it
        # would break the common case to serve the CI one.
        attempts.append(
            (
                "detached+new_process_group+breakaway_from_job",
                {"creationflags": detached | new_group | breakaway},
            )
        )
    else:
        attempts.append(("start_new_session", {"start_new_session": True}))

    last_error: BaseException | None = None
    for name, extra in attempts:
        # Opened per attempt: a failed spawn must not leave a descriptor behind,
        # and the child owns its own duplicate once it exists.
        try:
            stream = open(log_path, "ab", buffering=0)
        except OSError as exc:
            raise SupervisorError(
                f"could not open the sidecar log {log_path}: {exc}"
            ) from exc
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=stream,
                cwd=working_directory,
                close_fds=True,
                **extra,  # type: ignore[arg-type]
            )
        except OSError as exc:
            last_error = exc
            stream.close()
            continue
        finally:
            with contextlib.suppress(OSError):
                stream.close()

        # A job object that kills detached children does it immediately, so a
        # very short grace period distinguishes "died on creation" from "still
        # starting up" without waiting out the whole startup timeout.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(POLL_INTERVAL_SECONDS / 2)
        if process.poll() is None:
            return process, name
        last_error = SupervisorError(
            f"the sidecar exited immediately with status {process.returncode}"
        )

    raise SupervisorError(
        f"could not spawn {GATEWAY_MODULE}: {last_error}. See {log_path} for the "
        f"sidecar's own output."
    )


def start(
    config: AutoRouterConfig | None = None,
    *,
    timeout: float | None = None,
) -> Status:
    """Start the sidecar and wait until it answers ``/healthz``.

    Already running is a no-op returning the existing :class:`Status`, not an
    error: the session-start hook calls this unconditionally and a second
    Hermes session must not fail because the first one already started the
    gateway.

    Raises:
        SupervisorError: the port is held by something this install does not
            own, the spawn failed, or the gateway did not answer in time.
    """
    config = _resolve_config(config)
    logger = get_logger("hermes_auto.supervisor")
    startup_timeout = (
        float(config.gateway.startup_timeout_seconds) if timeout is None else timeout
    )

    current = status(config)
    if current.running:
        return current
    if current.kind in (STATUS_FOREIGN, STATUS_UNRESPONSIVE, STATUS_CORRUPT):
        raise SupervisorError(
            f"refusing to start: {current.detail}"
        )

    process, strategy = _spawn(config)
    logger.info(
        {
            "event": "supervisor.spawned",
            "pid": process.pid,
            "strategy": strategy,
            "log": str(gateway_log_path(config)),
        }
    )

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SupervisorError(
                f"the sidecar exited during startup with status "
                f"{process.returncode}. See {gateway_log_path(config)}."
            )
        candidate = status(config)
        if candidate.running:
            logger.info(
                {
                    "event": "supervisor.started",
                    "instance_id": candidate.instance_id,
                    "port": candidate.port,
                    "strategy": strategy,
                }
            )
            return dataclasses.replace(
                candidate, detail=f"{candidate.detail} [spawn: {strategy}]"
            )
        time.sleep(POLL_INTERVAL_SECONDS)

    with contextlib.suppress(Exception):
        process.kill()
    raise SupervisorError(
        f"the gateway did not answer /healthz within {startup_timeout}s of "
        f"starting. See {gateway_log_path(config)} for the sidecar's own output."
    )


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def _request_admin_shutdown(
    host: str, admin_port: int, token: str | None
) -> tuple[bool, str]:
    """POST ``/admin/v1/shutdown``. Returns ``(accepted, detail)``.

    Never raises. Every failure here has a fallback rung below it, and an
    exception would skip them.
    """
    if admin_port == 0:
        return False, (
            "the runtime file records admin_port 0, the sentinel for 'no admin "
            "listener'; graceful drain is unreachable for this process"
        )
    if not token:
        return False, (
            "no admin token is available, so the shutdown endpoint would answer "
            "401; run `hermes-auto setup` to mint one"
        )

    request = urllib.request.Request(
        f"http://{authority(host, admin_port)}/admin/v1/shutdown", method="POST"
    )
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(
            request, timeout=ADMIN_REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = response.read(64 * 1024)
            code = response.status
    except urllib.error.HTTPError as exc:
        return False, f"the admin shutdown endpoint answered {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"the admin shutdown endpoint could not be reached ({exc})"

    signalled: object = "unknown"
    with contextlib.suppress(Exception):
        signalled = json.loads(body.decode("utf-8")).get("servers_signalled")
    if signalled == 0:
        # The endpoint reports this honestly rather than pretending; treat it as
        # a failed graceful rung so the escalation still runs.
        return False, "the admin API accepted the request but signalled 0 servers"
    return True, f"admin shutdown accepted ({code}, servers_signalled={signalled})"


def _instance_gone(config: AutoRouterConfig, host: str, record: RuntimeFile) -> bool:
    """Has the *recorded* gateway instance stopped running?

    Two local signals, no network traffic, in cost order:

    1. **The runtime file.** ``gateway/main.py`` clears it in a ``finally`` as it
       exits, so the file disappearing is the exiting process's own report that
       it has gone -- observed at 0.41 s on a graceful stop. A file naming a
       *different* ``instance_id`` means it has already been replaced.
    2. **The port is bindable.** Covers the hard-kill path, where the process
       never got to clear anything.

    Deliberately no HTTP polling. uvicorn's graceful shutdown ends with
    ``while self.server_state.connections: await asyncio.sleep(0.1)``, so a
    health probe that opens a connection and then times out leaves exactly such
    a connection behind and *prevents the drain from completing* -- each poll
    feeding the next. Measured before this was understood: 13.2 s of entirely
    self-inflicted delay stopping a gateway that exits on its own in 0.41 s.
    """
    try:
        current = read_runtime(config.gateway.state_dir)
    except RuntimeFileError:
        return True
    if current is None or current.instance_id != record.instance_id:
        return True
    return _port_bindable(host, record.port)


def _wait_for_exit(
    config: AutoRouterConfig, host: str, record: RuntimeFile, deadline: float
) -> bool:
    """Poll until the recorded instance is gone, or the deadline passes."""
    while time.monotonic() < deadline:
        if _instance_gone(config, host, record):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return _instance_gone(config, host, record)


def _signal_pid(pid: int, *, hard: bool) -> tuple[bool, str]:
    """Send the terminate or kill signal to *pid*. Never raises.

    On POSIX these are two genuinely different rungs: ``SIGTERM`` is a request a
    process may handle, ``SIGKILL`` is not. On Windows there is only one rung --
    ``os.kill`` calls ``TerminateProcess`` whatever signal number it is handed --
    which is exactly why the admin shutdown above is the primary path and not a
    nicety.
    """
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGTERM)
            return True, "TerminateProcess sent (Windows has no graceful signal)"
        os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
        return True, "SIGKILL sent" if hard else "SIGTERM sent"
    except ProcessLookupError:
        return False, "the process had already exited"
    except PermissionError:
        return False, "the process is not owned by this user"
    except OSError as exc:
        return False, f"the signal could not be delivered ({exc})"


def stop(
    config: AutoRouterConfig | None = None,
    timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS,
) -> Status:
    """Stop the sidecar, gracefully if at all possible.

    Escalation, in order: ``POST /admin/v1/shutdown`` (the primary path on every
    platform, because Windows has no ``SIGTERM``) -> wait for ``/healthz`` to
    stop answering -> terminate -> kill. The runtime file is cleared only after
    the port has actually stopped answering; clearing it earlier would advertise
    a stopped gateway that is still draining a stream.

    Stopping a gateway that is not running returns cleanly. Stopping when a
    *foreign* process holds the recorded port raises, because the only thing
    this could do at that point is kill something it does not own.
    """
    config = _resolve_config(config)
    logger = get_logger("hermes_auto.supervisor")
    host = probe_host(config)

    current = status(config)
    if current.kind in (STATUS_FOREIGN, STATUS_UNRESPONSIVE):
        raise SupervisorError(f"refusing to stop: {current.detail}")
    if not current.running:
        return current

    record = read_runtime(config.gateway.state_dir)
    if record is None:  # pragma: no cover - lost a race with another stop
        return status(config)

    # Imported here rather than at module scope: this pulls in the gateway
    # package (and Starlette beneath it) purely to reach the admin token's
    # filename and permission readback, and `status` must stay cheap for the
    # session-start hook that calls it on every Hermes launch.
    from .gateway.admin import read_admin_token

    admin_token: str | None
    try:
        admin_token = read_admin_token(config.gateway.state_dir)
    except Exception:  # noqa: BLE001 - a damaged token file must not block stop
        admin_token = None

    deadline = time.monotonic() + timeout
    accepted, detail = _request_admin_shutdown(host, record.admin_port, admin_token)
    logger.info(
        {
            "event": "supervisor.shutdown.requested",
            "graceful": accepted,
            "detail": detail,
            "admin_port": record.admin_port,
        }
    )

    steps = [f"admin shutdown: {detail}"]
    if accepted and _wait_for_exit(config, host, record, deadline):
        return _finish_stop(config, record, steps, logger)

    # Escalate -- but only against a process whose identity we have *just*
    # re-confirmed. A stale runtime file must never be able to cause a signal.
    for hard in (False, True):
        if _instance_gone(config, host, record):
            return _finish_stop(config, record, steps, logger)
        outcome, answered = _probe_health(host, record.port)
        if outcome != _PROBE_OK or answered != record.instance_id:
            steps.append(
                "escalation abandoned: the recorded instance_id no longer "
                "answers on that port, so the recorded pid may belong to an "
                "unrelated process now"
            )
            break
        sent, why = _signal_pid(record.pid, hard=hard)
        steps.append(f"{'kill' if hard else 'terminate'}: {why}")
        if not sent:
            break
        if _wait_for_exit(config, host, record, max(deadline, time.monotonic() + 2.0)):
            return _finish_stop(config, record, steps, logger)

    final = status(config)
    if final.running:
        raise SupervisorError(
            "the gateway is still answering after the full stop sequence: "
            + "; ".join(steps)
        )
    return _finish_stop(config, record, steps, logger)


def _finish_stop(
    config: AutoRouterConfig,
    record: RuntimeFile,
    steps: list[str],
    logger: object,
) -> Status:
    """Clear the runtime file and report. Called only once the port is quiet.

    Identity-scoped: by the time a drain finishes, a replacement gateway may
    already have published its own file, and removing that one would orphan it.
    """
    with contextlib.suppress(RuntimeFileError):
        _clear_runtime_if_ours(config.gateway.state_dir, record.instance_id)
    port = record.port
    detail = "stopped (" + "; ".join(steps) + ")"
    logger.info(  # type: ignore[attr-defined]
        {"event": "supervisor.stopped", "port": port, "steps": steps}
    )
    return Status(
        running=False,
        instance_id=None,
        port=port,
        pid=None,
        detail=detail,
        kind=STATUS_NOT_STARTED,
    )


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


def restart(
    config: AutoRouterConfig | None = None,
    *,
    timeout: float | None = None,
) -> Status:
    """Stop, start, and wait for the ``instance_id`` to actually change.

    Waiting for the identity to *change* rather than for ``/healthz`` to answer
    is the whole point. During a graceful drain the old process still answers,
    so a restart that returned on the first successful probe would routinely
    return the process it was asked to replace -- and "restart is safe
    mid-session and leaves no stale PID" would be an assertion rather than a
    tested claim.
    """
    config = _resolve_config(config)
    startup_timeout = (
        float(config.gateway.startup_timeout_seconds) if timeout is None else timeout
    )

    before = status(config)
    previous_instance_id = before.instance_id if before.running else None

    stop(config)
    started = start(config, timeout=startup_timeout)

    if previous_instance_id is None or started.instance_id != previous_instance_id:
        return started

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        candidate = status(config)
        if candidate.running and candidate.instance_id != previous_instance_id:
            return candidate
        time.sleep(POLL_INTERVAL_SECONDS)

    raise SupervisorError(
        f"restart did not produce a new gateway within {startup_timeout}s: the "
        f"instance_id is still {previous_instance_id}, so the process that was "
        f"asked to stop is the one still answering."
    )
