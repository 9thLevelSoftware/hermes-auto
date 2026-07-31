"""The local admin API: a second listener, secret, and trust scope.

Possession of the inference bearer token grants no admin capability. The
authenticated surface exposes health, bounded decision explanations, and
graceful shutdown. It has no CORS middleware and no routing mutation, feedback,
or telemetry endpoints.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import secrets
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import AutoRouterConfig, load_config
from ..routing import DecisionRouter
from ..state.paths import state_dir as resolve_state_dir
from ..state.runtime import RuntimeFileError, read_runtime
from ..telemetry.redaction import get_logger, redact, sanitize_url
from .auth import AuthError, compare_token, permissions_ok, secure_write
from .errors import (
    GatewayError,
    error_response,
    gateway_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
)

__all__ = [
    "ADMIN_TOKEN_FILENAME",
    "ADMIN_TOKEN_ENTROPY_BYTES",
    "AdminAuthMiddleware",
    "admin_token_path",
    "admin_token_permissions_ok",
    "create_admin_app",
    "mint_admin_token",
    "read_admin_token",
    "require_admin",
]

#: Sits beside ``<state_dir>/token``, and is emphatically not it. The name is
#: part of the on-disk contract plan 02-07's ``stop`` and ``doctor`` read.
ADMIN_TOKEN_FILENAME: str = "admin-token"

#: Bytes of entropy, matching ``gateway.auth.TOKEN_ENTROPY_BYTES``. The argument
#: to ``secrets.token_urlsafe`` is entropy, not output length: 32 bytes yields 43
#: characters.
ADMIN_TOKEN_ENTROPY_BYTES: int = 32

#: One 401 message for every failure mode: absent header, malformed header, wrong
#: token, inference token, no admin token file. It names the file a legitimate
#: operator can read and says nothing about which of those cases occurred.
_DENIED_MESSAGE = (
    "Admin authentication failed. The admin API is a separate authentication "
    "scope from the inference API: present the bearer token from "
    f"<state_dir>/{ADMIN_TOKEN_FILENAME}, not the inference token."
)

#: Compared against when no admin token exists, purely so that the absent-token
#: path performs the same work as the wrong-token path. Never written to disk,
#: never accepted -- the absent case denies unconditionally regardless of the
#: comparison's result.
_DECOY_TOKEN = secrets.token_urlsafe(ADMIN_TOKEN_ENTROPY_BYTES)

# ---------------------------------------------------------------------------
# The admin token: a separate secret, in a separate file
# ---------------------------------------------------------------------------


def admin_token_path(
    configured: str | os.PathLike[str] | None = None,
    *,
    create: bool = True,
) -> pathlib.Path:
    """Resolve ``<state_dir>/admin-token``.

    ``create`` refers to the containing directory only, never to the token file,
    which exists solely so that :func:`mint_admin_token` can create it with
    restrictive permissions from the first byte.
    """
    return resolve_state_dir(configured, create=create) / ADMIN_TOKEN_FILENAME


def mint_admin_token(
    configured: str | os.PathLike[str] | None = None,
) -> str:
    """Generate the admin-scope token, store it restrictively, and return it.

    Uses ``gateway.auth.secure_write``, the project's single implementation of
    per-platform restrictive creation, rather than re-deriving
    ``os.open(..., 0o600)`` and ``icacls`` here. Three copies of platform
    permission logic would drift, and the two that drifted would be the two
    nobody re-verified.

    Like ``auth.mint_token``, this does not raise when the permission readback
    fails: a gateway that refused to start on a machine without ``icacls`` pushes
    the operator toward running it some other way, which is strictly less safe.
    :func:`admin_token_permissions_ok` is the authority, and ``doctor`` reports
    it.
    """
    token = secrets.token_urlsafe(ADMIN_TOKEN_ENTROPY_BYTES)
    secure_write(admin_token_path(configured), token.encode("ascii"))
    return token


def read_admin_token(
    configured: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return the stored admin token, or ``None`` if none has been minted.

    Absent is not corrupt, the same distinction the runtime file draws: no file
    means "the admin scope has not been set up", while a file that exists and is
    empty or undecodable means something damaged it and is an ``AuthError``. The
    authentication path treats both as "deny", but ``doctor`` needs to tell them
    apart.
    """
    path = admin_token_path(configured, create=False)
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


def admin_token_permissions_ok(
    configured: str | os.PathLike[str] | None = None,
) -> tuple[bool, str]:
    """Read back the effective permissions of the admin token file."""
    return permissions_ok(admin_token_path(configured, create=False))


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


def _bearer(header: str | None) -> str:
    """Extract the bearer credential, returning ``""`` for anything unusable.

    A sentinel rather than an exception, so that absent and malformed stay on the
    same code path as wrong. The empty string is then compared like any other
    candidate and loses -- except against an empty expected token, which is why
    :func:`require_admin` branches on that case explicitly.
    """
    if not header:
        return ""
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return credential.strip()


def _denied() -> JSONResponse:
    return error_response(
        401,
        _DENIED_MESSAGE,
        "invalid_request_error",
        code="invalid_admin_token",
    )


def _expected_admin_token(configured: Any) -> str | None:
    """The admin token as it is on disk right now, or ``None``.

    Read per request rather than cached at startup. Admin traffic is a handful of
    calls from ``status``, ``doctor`` and ``stop`` -- not a hot path -- and a
    fresh read means an operator who re-mints the token does not have to restart
    the gateway to make it take effect, and that deleting the file takes effect
    immediately. An ``AuthError`` (empty or non-ASCII file) is deliberately
    flattened to ``None`` here: a damaged token file must deny, not 500.
    """
    try:
        return read_admin_token(configured)
    except AuthError:
        return None


def require_admin(
    request: Request,
    configured: Any = None,
) -> JSONResponse | None:
    """Return a 401 response unless *request* carries the admin token.

    Returns ``None`` on success, so a caller reads as
    ``denied = require_admin(request); if denied is not None: return denied``.
    :class:`AdminAuthMiddleware` calls this for every request into the admin app
    so that no handler has to.

    The response never reveals which token was expected, whether an admin token
    exists, or whether the credential presented was the *inference* token -- all
    five failure modes produce one identical body.

    Args:
        request: The inbound request.
        configured: ``gateway.state_dir``. Read from ``request.app.state`` when
            omitted.
    """
    if configured is None:
        configured = getattr(request.app.state, "state_dir", None)

    supplied = _bearer(request.headers.get("authorization"))
    expected = _expected_admin_token(configured)

    if not expected:
        # `hmac.compare_digest(b"", b"")` is True, so falling through to the
        # comparison below with an empty expected token would authenticate a
        # request that sent no credential at all. Deny unconditionally -- but
        # still pay the comparison, so "no admin token file" and "wrong token"
        # do not separate under timing on a shared machine.
        compare_token(supplied, _DECOY_TOKEN)
        return _denied()

    if not compare_token(supplied, expected):
        return _denied()
    return None


class AdminAuthMiddleware:
    """Authenticate every request into the admin app before it is routed.

    Middleware rather than a decorator or a per-handler call, because the failure
    mode being designed out is a future route that forgets to authenticate. An
    unknown path is answered 401 rather than 404 for the same reason a wrong
    token and an absent one share a message: an unauthenticated caller learns
    nothing about the admin surface, not even which parts of it exist.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Nothing but HTTP is served here. A websocket or lifespan message
            # is passed through: lifespan must reach the app, and there is no
            # websocket route to reach.
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        denied = require_admin(request)
        if denied is not None:
            # Starlette sets scope["app"] before the middleware stack runs.
            host_app = scope.get("app")
            logger = getattr(getattr(host_app, "state", None), "logger", None)
            if logger is not None:
                # Path and method only. Never the header, never the credential:
                # `authorization` is on the redaction sink's banned list and a
                # near-miss spelling would not be caught by it.
                logger.warning(
                    {
                        "event": "admin.auth.denied",
                        "path": scope.get("path", ""),
                        "method": scope.get("method", ""),
                    }
                )
            await denied(scope, receive, send)
            return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


#: This module had the only correct implementation of userinfo stripping, which
#: is how the three sites that lacked one were found. It now lives in
#: ``telemetry.redaction`` so all four share it; the local name is kept because
#: it reads better at the call site below.
_sanitize_url = sanitize_url


def _uptime_seconds(started_at: str | None) -> float | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - started).total_seconds()
    return round(max(delta, 0.0), 3)


async def status(request: Request) -> Response:
    """``GET /admin/v1/status`` -- what this process is and what it is talking to.

    ``instance_id`` comes from the runtime file, unlike ``/healthz``'s, which
    reports the id of the process answering. That difference is deliberate and
    load-bearing: plan 02-07 defeats PID reuse by comparing the two, and a
    ``/healthz`` that read the file would make the mismatch branch unreachable.
    Here the file's value is the right one -- an operator asking "what does the
    supervisor think is running?" is asking about the file.

    The payload is assembled from a fixed key set and then passed through
    ``redact()`` as a second barrier. Neither token appears in it, and
    ``upstream_base_url`` is stripped of any userinfo before it is included.
    """
    state = request.app.state
    config: AutoRouterConfig = state.config

    instance_id: str | None = None
    port: int | None = None
    admin_port: int | None = None
    started_at: str | None = None
    try:
        runtime = read_runtime(config.gateway.state_dir)
    except RuntimeFileError:
        # A damaged runtime file is a diagnostic fact, not a reason to fail the
        # diagnostic endpoint. The nulls below say "unknown" and `doctor` is what
        # explains why.
        runtime = None
    if runtime is not None:
        instance_id = runtime.instance_id
        port = runtime.port
        admin_port = runtime.admin_port
        started_at = runtime.started_at
    if started_at is None:
        started_at = state.started_at

    payload = {
        "instance_id": instance_id,
        "pid": os.getpid(),
        "port": port,
        "admin_port": admin_port,
        "started_at": started_at,
        "uptime_seconds": _uptime_seconds(started_at),
        "upstream_base_url": _sanitize_url(
            config.resolved_candidates[0].base_url
        ),
        "configured_candidate_count": len(config.resolved_candidates),
        "most_recent_decision": (
            state.router.latest_decision().public()
            if state.router.latest_decision() is not None
            else None
        ),
        "strict_validation": config.gateway.strict_validation,
    }
    return JSONResponse(redact(payload, directory=state.salt_dir))


async def decision(request: Request) -> Response:
    """Return a prompt-free explanation for a session or the latest route."""
    session_id = request.path_params.get("session_id", "")
    router: DecisionRouter = request.app.state.router
    if session_id == "latest":
        found = router.latest_decision()
    else:
        found = router.decision_for(str(session_id))
    if found is None:
        return error_response(
            404,
            "No in-memory routing decision is available for that session.",
            "invalid_request_error",
            code="decision_not_found",
        )
    return JSONResponse(found.public())


def _running_uvicorn_servers() -> list[Any]:
    """Every ``uvicorn.Server`` driving this event loop.

    ``main.py`` constructs both servers itself and hands their ``serve()``
    coroutines to ``asyncio.gather``; there is no registry, no reference on the
    ASGI scope, and no way to reach them from a handler through a supported API.
    They are reachable exactly once through the running tasks: ``serve()`` is
    the outermost coroutine of each task, and its frame's ``self`` is the server.

    Deliberately not signal-based. ``signal.raise_signal(SIGINT)`` would reach
    uvicorn's captured handler under ``main.py``, but under any host that has not
    installed one -- Starlette's ``TestClient``, for instance -- it raises
    ``KeyboardInterrupt`` into whatever is running, turning a graceful stop into
    a crash. Returning an empty list when no server is found is the honest
    outcome, and the response says so.
    """
    try:
        import asyncio

        import uvicorn
    except ImportError:  # pragma: no cover - uvicorn is a runtime dependency
        return []

    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:  # pragma: no cover - no running loop
        return []

    servers: list[Any] = []
    for task in tasks:
        coro = task.get_coro()
        frame = getattr(coro, "cr_frame", None)
        if frame is None:
            continue
        candidate = frame.f_locals.get("self")
        if isinstance(candidate, uvicorn.Server) and not any(
            candidate is seen for seen in servers
        ):
            servers.append(candidate)
    return servers


async def shutdown(request: Request) -> Response:
    """``POST /admin/v1/shutdown`` -- 202, then a graceful drain.

    The primary stop path on every platform. Windows has no ``SIGTERM``, so plan
    02-07's ``stop()`` cannot rely on signalling and POSTs here first; everything
    after this in its sequence -- ``terminate()``, then ``kill()`` -- severs
    in-flight streams.

    Setting ``should_exit`` is uvicorn's graceful path, not a synonym for
    stopping: the server leaves its main loop, closes the listening socket,
    asks each open connection to close *after* its current response, and then
    waits for outstanding tasks with no timeout configured. A streaming
    completion in flight therefore finishes. This request finishes too, which is
    why the 202 arrives.

    202 rather than 200 because nothing here has happened yet when the response
    is written. Reporting ``servers_signalled`` keeps that honest in the one case
    where it is zero -- an admin app hosted by something other than uvicorn, in
    which case the caller must fall through to its next stop mechanism.
    """
    state = request.app.state
    state.shutdown_requested = True

    servers = _running_uvicorn_servers()
    for server in servers:
        server.should_exit = True

    requested = state.request_exit
    if requested is not None:
        requested()

    state.logger.info(
        {
            "event": "admin.shutdown.requested",
            "servers_signalled": len(servers),
            "external_hook": requested is not None,
        }
    )
    return JSONResponse(
        {
            "status": "accepted",
            "detail": (
                "graceful drain started: the gateway has stopped accepting new "
                "connections and will exit once in-flight requests, including "
                "streaming completions, have finished"
            ),
            "servers_signalled": len(servers),
        },
        status_code=202,
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def create_admin_app(
    config: AutoRouterConfig | None = None,
    *,
    request_exit: Callable[[], None] | None = None,
    decision_router: DecisionRouter | None = None,
) -> Starlette:
    """Build the admin ASGI app. Pure -- no sockets, no token read, no I/O.

    Everything with a cost or a side effect happens in the lifespan handler, so
    a verification command, a test, and a documentation generator can all call
    this without a state directory or a running gateway.

    Args:
        config: Loaded configuration. ``load_config(None)`` when omitted, which
            falls through to built-in defaults when no config file exists.
        request_exit: Optional callable invoked by ``POST /admin/v1/shutdown`` in
            addition to signalling every uvicorn server in the process.
            ``admin_main.py`` passes its own server's stop hook; ``main.py`` does
            not pass anything and is covered by server discovery.

    Returns:
        A ``Starlette`` app serving only ``/admin/v1/*``. It has no
        ``/v1/chat/completions`` and no ``/v1/models``: the admin surface and the
        inference surface are separate listeners on separate scopes, and merging
        them is the failure this module exists to prevent.
    """
    if config is None:
        config = load_config(None)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        directory = resolve_state_dir(config.gateway.state_dir)
        app.state.salt_dir = directory
        app.state.logger = get_logger(
            "hermes_auto.gateway.admin", directory=directory
        )

        # Mint the admin token if it is absent, exactly as the inference app
        # mints its own. Not doing so would leave a supervised gateway whose
        # every admin call 401s, which makes graceful shutdown unreachable and
        # silently degrades plan 02-07's `stop` into a hard kill on the one
        # platform that has no SIGTERM. The token is read per request, so an
        # operator deleting the file still gets fail-closed behavior and the
        # listener still answers -- which is what lets `doctor` say why.
        if _expected_admin_token(config.gateway.state_dir) is None:
            mint_admin_token(config.gateway.state_dir)
            app.state.logger.info({"event": "admin.token.minted"})

        ok, reason = admin_token_permissions_ok(config.gateway.state_dir)
        if not ok:
            app.state.logger.warning(
                {"event": "admin.token.permissions", "detail": reason}
            )

        app.state.logger.info({"event": "admin.started"})
        try:
            yield
        finally:
            app.state.logger.info({"event": "admin.stopped"})

    routes = [
        Route("/admin/v1/status", status, methods=["GET"]),
        Route(
            "/admin/v1/decisions/{session_id}",
            decision,
            methods=["GET"],
        ),
        Route("/admin/v1/shutdown", shutdown, methods=["POST"]),
    ]
    app = Starlette(
        routes=routes,
        # AdminAuthMiddleware only. No CORSMiddleware, now or ever: a permissive
        # origin policy on a loopback control API makes every page in the
        # operator's browser a caller that can shut the router down.
        middleware=[Middleware(AdminAuthMiddleware)],
        exception_handlers={
            GatewayError: gateway_error_handler,
            HTTPException: http_exception_handler,
            Exception: unhandled_exception_handler,
        },
        lifespan=lifespan,
    )

    # Set before startup so create_admin_app()'s result is inspectable and so a
    # handler reached without a lifespan fails on a 401 rather than an
    # AttributeError.
    app.state.config = config
    app.state.router = decision_router or DecisionRouter(config.resolved_candidates)
    app.state.state_dir = config.gateway.state_dir
    app.state.salt_dir = None
    app.state.request_exit = request_exit
    app.state.shutdown_requested = False
    app.state.started_at = datetime.now(timezone.utc).isoformat()
    app.state.logger = get_logger("hermes_auto.gateway.admin")
    return app
