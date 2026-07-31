"""The supervised entry point: bind, publish, serve -- strictly in that order.

Run as ``python -m hermes_auto.gateway.main``. This is the process plan 02-07
starts, supervises, and stops.

**Ordering is the contract.** The runtime file is written *after* both listening
sockets are bound and never before. Every consumer treats that file's existence
as "there should be something answering on that port": ``status`` reports a live
gateway, the control plugin's session-start hook stops waiting, and Hermes points
its client at the URL. Writing it first turns a slow or failed bind into a
gateway that is advertised and unreachable -- the exact race the supervision
contract in ``02-CONTEXT.md`` was written to close. Binding by hand rather than
letting uvicorn do it is what makes the ordering expressible at all.

**One process, two listeners.** The inference app and the admin app share one
asyncio loop in one process. This is load-bearing rather than tidy: ``stop()``
POSTs ``/admin/v1/shutdown`` as its primary path *on every platform*, because
Windows has no ``SIGTERM``. An admin listener that no supervised process starts
would make graceful drain unreachable and silently degrade every stop into a hard
kill, cutting in-flight streams. ``admin_main.py`` is a debug-only entry that
nothing supervises, so it cannot stand in for this.

``create_admin_app`` belongs to plan 02-06 and is imported lazily, inside the
function that needs it. If it is absent the gateway serves inference only, says
so at WARNING, and records ``admin_port: 0`` -- a sentinel meaning "no admin
listener", chosen over recording a port nothing is listening on, which would send
every ``stop()`` to a closed socket and look like a hung shutdown.

**Loopback only, and a non-loopback address is a startup error.** Not a warning.
A warning on an unattended sidecar is a message nobody reads, and the failure it
precedes is a locally-authenticated inference endpoint exposed to the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import socket
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import uvicorn

from ..config import AutoRouterConfig, ConfigError, load_config
from ..state.paths import state_dir as resolve_state_dir
from ..state.runtime import (
    RuntimeFile,
    RuntimeFileError,
    clear_runtime,
    new_instance_id,
    read_runtime,
    write_runtime,
)
from ..telemetry.redaction import get_logger
from .app import create_app

__all__ = ["BIND_HOST", "bind_socket", "main", "require_loopback", "serve"]

#: The only address this gateway binds by default, and the only shape of address
#: it accepts at all. design.md 16's ``gateway.url`` is
#: ``http://127.0.0.1:8787``.
BIND_HOST = "127.0.0.1"

#: How often the two servers check whether the other has been asked to stop.
#: uvicorn's shutdown signal is a plain attribute with no event to await, so a
#: short poll is the version-independent way to link them.
_EXIT_POLL_SECONDS = 0.05


class StartupError(Exception):
    """The gateway cannot start. Raised before anything is published."""


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def require_loopback(url: str) -> str:
    """Return the host from *url*, or raise unless it is a loopback address.

    ``localhost`` is accepted by name: it is the one hostname whose loopback
    meaning is guaranteed by convention on every platform this project targets.
    Any other name is refused rather than resolved -- a DNS lookup that returns a
    routable address would have already exposed the port by the time anything
    noticed.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise StartupError(
            f"gateway.url has no host: {url!r}. Expected something like "
            f"http://{BIND_HOST}:8787."
        )
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise StartupError(
            f"gateway.url host {host!r} is not a loopback address. The gateway "
            f"binds loopback only: use {BIND_HOST}, ::1, or localhost. Exposing "
            f"the inference port to a network would put a locally-authenticated "
            f"endpoint on it."
        ) from exc
    if not address.is_loopback:
        raise StartupError(
            f"gateway.url host {host!r} is not a loopback address. The gateway "
            f"binds loopback only: use {BIND_HOST}, ::1, or localhost."
        )
    return host


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind and listen, returning the socket. Raises ``StartupError`` on failure.

    ``SO_REUSEADDR`` is set on POSIX and deliberately **not** on Windows, where
    its semantics differ: on Windows it lets a second process bind a port another
    process is already listening on, so the two split incoming connections
    unpredictably. Here that would mean a stale gateway silently sharing the
    inference port with a new one.
    """
    infos = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )
    family, socktype, proto, _, address = infos[0]
    sock = socket.socket(family, socktype, proto)
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(address)
        sock.listen(128)
    except OSError as exc:
        sock.close()
        raise StartupError(
            f"could not bind {host}:{port} ({type(exc).__name__}: {exc}). Another "
            f"gateway may already be running; check the runtime file."
        ) from exc
    sock.set_inheritable(False)
    return sock


def _port_of(sock: socket.socket) -> int:
    return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


class _SecondaryServer(uvicorn.Server):
    """A uvicorn server that does not touch process signal handlers.

    Only one server in the process may install them: uvicorn's
    ``capture_signals`` saves and restores the handlers it replaces, so two
    nested captures leave only the inner one active and a Ctrl-C would stop the
    admin listener while inference kept running.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Any:
        yield


def _make_server(app: Any, *, primary: bool) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        log_level="info",
        # Explicit rather than "auto": the app declares a lifespan and a silent
        # downgrade to "off" would skip validator hoisting, the token read, and
        # the upstream client, surfacing as an AttributeError on request one.
        lifespan="on",
        access_log=False,
        # No date/server header noise in a byte-comparison harness's way, and one
        # less thing advertising the stack to anything that reaches the port.
        server_header=False,
        date_header=True,
    )
    return (uvicorn.Server if primary else _SecondaryServer)(config)


async def _mirror_exit(servers: list[uvicorn.Server]) -> None:
    """Stop every server as soon as any one of them is asked to stop.

    Covers both directions that matter: a signal captured by the inference
    server, and plan 02-06's ``POST /admin/v1/shutdown`` setting ``should_exit``
    on whichever server object it can reach.
    """
    while True:
        if any(server.should_exit for server in servers):
            for server in servers:
                server.should_exit = True
            return
        await asyncio.sleep(_EXIT_POLL_SECONDS)


def _build_admin_app(
    config: AutoRouterConfig, logger: Any, decision_router: Any = None
) -> Any | None:
    """Return plan 02-06's admin app, or None with a warning if it is absent.

    Imported here rather than at module scope so this plan does not block on
    02-06 landing, and so an error inside the admin app cannot stop the
    inference listener from coming up.
    """
    try:
        from .admin import create_admin_app  # type: ignore[attr-defined]
    except ImportError:
        logger.warning(
            {
                "event": "admin.unavailable",
                "reason": "hermes_auto.gateway.admin.create_admin_app not importable",
                "consequence": "graceful drain via POST /admin/v1/shutdown is "
                "unreachable; stop() will fall back to terminate/kill",
            }
        )
        return None
    try:
        return create_admin_app(config, decision_router=decision_router)
    except TypeError:
        return create_admin_app()


def _clear_own_runtime(
    configured: str | os.PathLike[str] | None,
    instance_id: str,
    logger: Any,
) -> None:
    """Retract the advertisement, but only while it still names *this* process.

    ``clear_runtime`` unlinks whatever happens to be at the path. That is only
    safe for a process that can show the advertisement is its own, and two
    guards together establish that. This function is the second; the first is
    ``published`` in :func:`serve`, which decides whether this is called at all.

    Both are load-bearing, and the first one is the observed incident. A second
    gateway whose inference port was free but whose *admin* port collided failed
    at ``bind_socket`` -- **inside** ``serve()``'s ``try``, unlike the inference
    bind above it -- and its ``finally`` deleted the runtime file of the healthy
    gateway already serving on that admin port. That gateway kept answering with
    nothing on disk naming it: ``status`` reported ``not_started`` and ``stop``
    had nothing to reap it by, so it had to be killed by hand after three hours.

    The identity compare closes what ``published`` alone leaves open: between
    this process writing its file and reaching here, a replacement may already
    have published its own, and unlinking that one recreates the same orphan by
    a different route. Read-compare-unlink is not atomic -- POSIX and Windows
    offer no portable compare-and-unlink -- so this narrows the window from the
    whole process lifetime to a single file read rather than closing it. Stated
    rather than claimed closed, the same way ``supervisor.stop`` states the
    residual window on its own identity re-check.
    """
    try:
        current = read_runtime(configured)
    except RuntimeFileError:
        # Present but unparseable. Deliberately left alone for the same reason
        # `status` leaves it: something wrote nonsense there and deleting the
        # evidence is how that bug survives.
        return
    if current is None:
        return
    if current.instance_id != instance_id:
        logger.info(
            {
                "event": "gateway.runtime_file.not_ours",
                "instance_id": instance_id,
                "file_instance_id": current.instance_id,
                "consequence": "left in place; it advertises a different process",
            }
        )
        return
    clear_runtime(configured)


async def serve(
    config: AutoRouterConfig,
    *,
    instance_id: str | None = None,
) -> int:
    """Bind, publish the runtime file, then serve until asked to stop."""
    directory = resolve_state_dir(config.gateway.state_dir)
    logger = get_logger("hermes_auto.gateway.main", directory=directory)

    host = require_loopback(config.gateway.url)
    resolved_instance_id = instance_id or new_instance_id()

    inference_socket = bind_socket(host, config.gateway.port)
    admin_socket: socket.socket | None = None
    servers: list[uvicorn.Server] = []
    sockets: list[socket.socket] = [inference_socket]

    #: Whether *this* process ever advertised itself. The ``finally`` below may
    #: run without a single line of the block having succeeded -- the admin bind
    #: raises from inside it -- and a process that never published must never
    #: retract. See :func:`_clear_own_runtime`.
    published = False

    try:
        from ..routing import DecisionRouter

        decision_router = DecisionRouter(config.resolved_candidates)
        admin_app = _build_admin_app(config, logger, decision_router)
        if admin_app is not None:
            admin_socket = bind_socket(host, config.gateway.admin_port)
            sockets.append(admin_socket)

        inference_port = _port_of(inference_socket)
        # 0 is the sentinel for "no admin listener". See the module docstring.
        admin_port = _port_of(admin_socket) if admin_socket is not None else 0

        # ---- both binds have succeeded; only now is the gateway advertised ----
        runtime_path = write_runtime(
            RuntimeFile(
                pid=os.getpid(),
                port=inference_port,
                admin_port=admin_port,
                instance_id=resolved_instance_id,
                started_at=datetime.now(timezone.utc).isoformat(),
                exe=sys.executable,
            ),
            config.gateway.state_dir,
        )
        published = True
        logger.info(
            {
                "event": "gateway.bound",
                "host": host,
                "port": inference_port,
                "admin_port": admin_port,
                "instance_id": resolved_instance_id,
                "runtime_file": str(runtime_path),
            }
        )

        app = create_app(
            config,
            instance_id=resolved_instance_id,
            decision_router=decision_router,
        )
        servers.append(_make_server(app, primary=True))
        tasks = [servers[0].serve(sockets=[inference_socket])]
        if admin_app is not None and admin_socket is not None:
            servers.append(_make_server(admin_app, primary=False))
            tasks.append(servers[1].serve(sockets=[admin_socket]))

        watchdog = asyncio.create_task(_mirror_exit(servers))
        try:
            await asyncio.gather(*tasks)
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
    finally:
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.close()
        # Remove our own advertisement -- *ours*, and only if we made one. It
        # runs on the crash path too: a runtime file outliving its process is
        # what makes `status` report a gateway that is not there. The converse,
        # a process outliving its runtime file, is what makes `stop` unable to
        # reap a gateway that *is* there, and is why this is guarded twice.
        if published:
            with contextlib.suppress(Exception):
                _clear_own_runtime(
                    config.gateway.state_dir, resolved_instance_id, logger
                )
        logger.info(
            {
                "event": "gateway.exited",
                "instance_id": resolved_instance_id,
                "published": published,
            }
        )

    return 0


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Returns a process exit status; never raises."""
    del argv  # configuration comes from config.yaml and the environment
    logger = get_logger("hermes_auto.gateway.main")
    try:
        config = load_config(None)
    except ConfigError as exc:
        logger.error({"event": "config.invalid", "detail": str(exc)})
        return 2

    try:
        return asyncio.run(serve(config))
    except StartupError as exc:
        logger.error({"event": "gateway.startup_failed", "detail": str(exc)})
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
