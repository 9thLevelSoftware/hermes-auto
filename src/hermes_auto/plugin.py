"""Reference control-plugin behavior for commands, ``/auto``, and auto-start.

Production setup writes a dependency-free equivalent beneath the Hermes home
and points it at the absolute standalone executable. This in-package module
keeps the same command semantics available for direct tests and integrations.

**The session hook fails soft.** If auto-start fails, Hermes must still open.
A router that cannot start should degrade into an error on the next inference
request -- where the user is asking for something the router is needed for --
rather than preventing the agent from launching at all. Every exception is
caught and logged through the redaction sink.
"""

from __future__ import annotations

import io
from typing import Any

from . import commands
from .telemetry.redaction import get_logger

__all__ = ["HOOK_NAME", "PLUGIN_NAME", "on_session_start", "register", "slash_auto"]

#: Must equal the home-scoped plugin manifest and enabled-plugin name.
PLUGIN_NAME = "hermes-auto-control"

#: The literal name from ``VALID_HOOKS`` (``hermes_cli/plugins.py``). Not
#: assembled, not aliased -- an unknown hook name is accepted with a warning and
#: never fires.
HOOK_NAME = "on_session_start"


def _logger() -> Any:
    return get_logger("hermes_auto.plugin")


def slash_auto(raw_args: str = "") -> str:
    """Show the Auto overview or explain the latest in-memory decision."""
    argument = (raw_args or "").strip().split(" ")[0].lower()
    buffer = io.StringIO()
    if argument in ("", "status"):
        commands.cmd_overview(stream=buffer)
        return buffer.getvalue().strip()
    if argument == "explain":
        commands.cmd_explain(stream=buffer)
        return buffer.getvalue().strip()
    return (
        f"/auto {argument} is not available. Use `/auto`, `/auto status`, "
        "or `/auto explain`."
    )


def on_session_start(**kwargs: Any) -> None:
    """Start the gateway when ``gateway.auto_start`` is set and it is not up.

    Returns ``None`` in every case, including every failure. Hermes wraps hook
    callbacks in a ``try``/``except`` of its own, but relying on that would mean
    the failure surfaces as a warning about a misbehaving plugin rather than as
    a message naming the gateway -- and a hook that raises on a broken config
    would make every Hermes session slower to start for no benefit.
    """
    del kwargs  # the hook is fired with session metadata this plugin does not use
    logger = _logger()
    try:
        from . import supervisor
        from .config import load_config

        config = load_config(None)
        if not config.gateway.auto_start:
            return
        state = supervisor.status(config)
        if state.running:
            return
        if state.kind in (supervisor.STATUS_FOREIGN, supervisor.STATUS_CORRUPT):
            # Something else owns the port, or the runtime file is damaged.
            # Starting would either fail on the bind or, worse, race. Report and
            # let the next request produce a real error.
            logger.warning(
                {"event": "plugin.auto_start.skipped", "reason": state.kind}
            )
            return
        started = supervisor.start(config)
        logger.info(
            {
                "event": "plugin.auto_start.ok",
                "instance_id": started.instance_id,
                "port": started.port,
            }
        )
    except Exception as exc:  # noqa: BLE001 - a session must open regardless
        logger.warning(
            {
                "event": "plugin.auto_start.failed",
                "error": type(exc).__name__,
                "detail": str(exc),
            }
        )


def _setup_cli(parser: Any) -> None:
    """Populate ``hermes auto``'s subparser.

    The same function that builds the console script's subcommands, so
    ``hermes auto status`` and ``hermes-auto status`` cannot drift.
    """
    from .cli import add_subcommands

    add_subcommands(parser)


def _handle_cli(args: Any) -> int:
    """Dispatch ``hermes auto <verb>``.

    ``set_defaults(func=...)`` was applied by ``add_subcommands``, so this only
    has to cover the bare ``hermes auto`` case.
    """
    handler = getattr(args, "func", None)
    if handler is None:
        print(
            "usage: hermes auto {setup,configure,explain,start,stop,restart,status,doctor}\n"
            "Run `hermes auto doctor` for a full diagnostic."
        )
        return 2
    return int(handler(args))


def register(ctx: Any) -> None:
    """Register this plugin's surface with Hermes.

    Each registration is independent and guarded: a Hermes build that lacks one
    of these context methods must not cost the others. The loader records what
    was registered by diffing its registries around this call, so a partial
    registration is reported accurately rather than as a total failure.
    """
    logger = _logger()

    try:
        ctx.register_cli_command(
            "auto",
            help="Supervise and diagnose the hermes-auto-router sidecar",
            setup_fn=_setup_cli,
            handler_fn=_handle_cli,
            description=(
                "Start, stop, restart and diagnose the local routing gateway."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            {"event": "plugin.register.cli_failed", "detail": str(exc)}
        )

    try:
        ctx.register_command(
            "auto",
            slash_auto,
            description="Report hermes-auto-router gateway status",
            args_hint="status",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            {"event": "plugin.register.slash_failed", "detail": str(exc)}
        )

    try:
        ctx.register_hook(HOOK_NAME, on_session_start)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            {"event": "plugin.register.hook_failed", "detail": str(exc)}
        )
