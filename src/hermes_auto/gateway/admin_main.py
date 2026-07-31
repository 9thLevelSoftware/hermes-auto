"""Debug-only entry point that serves the admin API alone.

Run as ``python -m hermes_auto.gateway.admin_main``.

**This is not what supervision starts.** ``02-CONTEXT.md``'s supervision contract
is explicit: one process binds *both* listeners on one asyncio loop and writes
``port`` and ``admin_port`` to the runtime file only after both binds succeed.
``gateway/main.py`` is that process, and it already imports
``create_admin_app`` -- so the admin listener plan 02-07's ``stop()`` POSTs to is
started by the supervised gateway, not by this module. Nothing supervises this
entry point and it writes no runtime file, because a runtime file naming an admin
port with no inference port behind it would advertise a gateway that cannot serve
a request.

What it is for: bringing the admin surface up in isolation to inspect it, and
proving the listener binds on its own. It also makes the loopback rule directly
testable without standing up an upstream.

**Loopback only, and a non-loopback address is a startup error, not a warning.**
``require_loopback`` is imported from ``main.py`` rather than re-derived: two
copies of "which addresses count as loopback" is one copy that will eventually
disagree, and the copy that drifted would be the one guarding the control API.
A warning would be worse than useless here -- on an unattended sidecar it is a
message nobody reads, and the failure it precedes is a *routing-control* API
exposed to the network.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import sys
from typing import Any

import uvicorn

from ..config import AutoRouterConfig, ConfigError, load_config
from ..state.paths import state_dir as resolve_state_dir
from ..telemetry.redaction import get_logger
from .admin import create_admin_app
from .main import BIND_HOST, StartupError, bind_socket, require_loopback

__all__ = [
    "BIND_HOST",
    "LOOPBACK_HOST",
    "main",
    "resolve_admin_port",
    "serve_admin",
]

#: The address ``design.md`` §16 pins (``gateway.url: http://127.0.0.1:8787``).
#: Checked against ``main.py``'s ``BIND_HOST`` at import rather than assumed: this
#: module re-exports that constant, and an edit there that widened it -- to
#: ``0.0.0.0`` for a container, say -- would silently widen the admin listener
#: too, and the admin listener is the one surface that can change routing
#: behavior at runtime. Failing at import is the loudest available failure and
#: happens before anything binds.
LOOPBACK_HOST: str = "127.0.0.1"

if BIND_HOST != LOOPBACK_HOST:  # pragma: no cover - guards a future edit
    raise ImportError(
        f"hermes_auto.gateway.main.BIND_HOST is {BIND_HOST!r}, expected "
        f"{LOOPBACK_HOST!r}. The admin API binds loopback only; refusing to "
        f"import rather than inherit a widened bind address."
    )


def resolve_admin_port(config: AutoRouterConfig) -> int:
    """The port the admin listener binds.

    ``gateway.admin_port`` defaults to ``gateway.port + 1`` in ``config.py``, and
    0 means "ask the OS for an ephemeral one" -- permitted only under
    ``HERMES_AUTO_TEST_EPHEMERAL=1``, which ``config.py`` enforces at load time.

    The admin port must differ from the inference port. ``config.py`` already
    rejects a config that sets them equal; this re-checks because this entry
    point can be handed a programmatically constructed config that never went
    through ``load_config``.
    """
    port = config.gateway.admin_port
    if port != 0 and port == config.gateway.port:
        raise StartupError(
            f"gateway.admin_port ({port}) must differ from gateway.port "
            f"({config.gateway.port}). The admin API is a separate "
            f"authentication scope on its own listener (design.md 5.3); sharing "
            f"a port with the inference API would defeat the separation."
        )
    return port


async def serve_admin(config: AutoRouterConfig) -> int:
    """Bind the admin app on loopback and serve until asked to stop."""
    directory = resolve_state_dir(config.gateway.state_dir)
    logger = get_logger("hermes_auto.gateway.admin_main", directory=directory)

    host = require_loopback(config.gateway.url)
    port = resolve_admin_port(config)

    admin_socket: socket.socket = bind_socket(host, port)
    bound_address = admin_socket.getsockname()
    bound_host, bound_port = str(bound_address[0]), int(bound_address[1])

    # Read the bound address back rather than trusting that validating the
    # configured one was enough. Same discipline as `gateway/auth.py` reading the
    # effective ACL back after `icacls`: a permission -- or here, a binding --
    # that has not been read back has not been established. `require_loopback`
    # accepts the name "localhost", whose resolution is a system question this
    # process does not control, and a resolver that returned a routable address
    # would have put the routing-control API on the network already.
    try:
        resolved = ipaddress.ip_address(bound_host)
    except ValueError:
        resolved = None
    if resolved is None or not resolved.is_loopback:
        admin_socket.close()
        raise StartupError(
            f"admin listener bound {bound_host}:{bound_port}, which is not a "
            f"loopback address. The admin API binds loopback only -- "
            f"{LOOPBACK_HOST} or ::1 -- because it is the one surface that can "
            f"change routing behavior at runtime."
        )

    server: Any = None

    def request_exit() -> None:
        if server is not None:
            server.should_exit = True

    app = create_admin_app(config, request_exit=request_exit)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="info",
            lifespan="on",
            access_log=False,
            server_header=False,
            date_header=True,
        )
    )

    logger.info(
        {
            "event": "admin.bound",
            "host": host,
            "admin_port": bound_port,
            "supervised": False,
            "note": "debug entry point; gateway.main binds both listeners",
        }
    )
    try:
        await server.serve(sockets=[admin_socket])
    finally:
        with contextlib.suppress(OSError):
            admin_socket.close()
        logger.info({"event": "admin.exited", "admin_port": bound_port})
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Returns a process exit status; never raises."""
    del argv  # configuration comes from config.yaml and the environment
    logger = get_logger("hermes_auto.gateway.admin_main")
    try:
        config = load_config(None)
    except ConfigError as exc:
        logger.error({"event": "config.invalid", "detail": str(exc)})
        return 2

    try:
        return asyncio.run(serve_admin(config))
    except StartupError as exc:
        logger.error({"event": "admin.startup_failed", "detail": str(exc)})
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
