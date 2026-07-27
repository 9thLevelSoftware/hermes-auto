"""The Hermes control plugin: ``hermes auto ...``, ``/auto status``, and a hook.

This module is the target of the ``hermes_agent.plugins`` entry point. Hermes
imports it and calls :func:`register` with a ``PluginContext``
(``hermes_cli/plugins.py``). Three facts about that loader shape everything
here.

**It registers the control plugin, never the provider.** Model providers are
discovered by scanning ``$HERMES_HOME/plugins/model-providers/``; the entry
point group is for general plugins only (02-CONTEXT § VERIFIED HERMES FACTS
item 1). The provider is a separate artifact, written by
``hermes_auto.hermes_shim.installer``. ``design.md`` §5.1 reads as though one
declaration does both, and following it produces a plugin that loads and a
provider that does not exist.

**The plugin does not load until it is enabled.** ``plugins.enabled`` is an
opt-in allow-list; an absent key means nothing is enabled, and ``register()`` is
then never called. ``hermes auto setup`` writes that key -- which is why
``setup`` must also be reachable through the ``hermes-auto`` console script, or
a fresh install has no way to run it.

**A wrong hook name fails silently.** ``register_hook`` warns about a name
outside ``VALID_HOOKS`` and then *stores the callback anyway*, where nothing
will ever call it. So the literal string ``"on_session_start"`` is used, taken
from ``VALID_HOOKS`` in the loader, and pinned by a test -- a typo here would
produce a plugin that loads cleanly, logs at WARNING into a file nobody reads,
and never auto-starts the gateway.

**Only ``/auto status`` is registered.** ``design.md`` §4.2 lists six further
``/auto`` subcommands. Every one of them reports or manipulates routing state,
and there is no routing in Phase 2 -- the gateway forwards every request to one
fixed upstream. A command that explained a routing decision today would be
inventing one that was never made, and a user who trusts it once will keep
trusting it after real routing lands and the answers change meaning. Leaving
them unregistered gives an honest "unknown command"; the phases that deliver
each of them are named in ``gateway/admin.py``'s 501 bodies. Which names are
withheld is pinned by ``tests/unit/test_cli.py``, since a docstring listing
them here is indistinguishable from code registering them to a text search.

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

#: Must equal the ``[project.entry-points."hermes_agent.plugins"]`` key: the
#: loader builds its manifest with ``name=ep.name`` and matches
#: ``plugins.enabled`` against that exact string.
PLUGIN_NAME = "hermes-auto-control"

#: The literal name from ``VALID_HOOKS`` (``hermes_cli/plugins.py``). Not
#: assembled, not aliased -- an unknown hook name is accepted with a warning and
#: never fires.
HOOK_NAME = "on_session_start"


def _logger() -> Any:
    return get_logger("hermes_auto.plugin")


def slash_auto(raw_args: str = "") -> str:
    """``/auto status`` -- the only slash command backed by real behaviour.

    Hermes's slash handlers take one raw argument string and return the text to
    show. Anything other than ``status`` (including bare ``/auto``) says so
    explicitly rather than defaulting to status, so a user who typed
    ``/auto explain`` learns the command does not exist yet instead of getting
    an answer to a different question.
    """
    argument = (raw_args or "").strip().split(" ")[0].lower()
    if argument in ("", "status"):
        buffer = io.StringIO()
        commands.cmd_status(stream=buffer)
        return buffer.getvalue().strip()
    return (
        f"/auto {argument} is not available. This build routes every request to "
        f"a single fixed upstream, so there is no routing decision to report, "
        f"change, or explain. Only `/auto status` is implemented."
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
            "usage: hermes auto {setup,start,stop,restart,status,doctor}\n"
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
