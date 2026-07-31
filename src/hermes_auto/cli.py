"""``hermes-auto`` -- argument parsing, and nothing else.

Deliberately thin. Every command body lives in :mod:`hermes_auto.commands`,
because the Hermes control plugin dispatches the same operations with no argv
to parse: if the behaviour lived here, the plugin would have to fabricate an
``argparse.Namespace`` to reach it, and the two entry points would drift.

This module is reachable two ways, and the difference matters on a fresh
install:

* **``hermes-auto ...``** -- the console script declared in
  ``[project.scripts]``. Available the moment the distribution is installed.
* **``hermes auto ...``** -- registered by the control plugin, which Hermes
  loads only when ``hermes-auto-control`` appears in ``plugins.enabled``.

``hermes auto setup`` is the command that writes that key, so on a first
install it cannot be the thing that runs it. The console script is the
bootstrap path out of that circle, which is why the ``[project.scripts]`` entry
is load-bearing rather than a convenience.
"""

from __future__ import annotations

import argparse
import sys

from . import commands
from .version import __version__

__all__ = ["build_parser", "main"]

_DESCRIPTION = (
    "Configure and supervise deterministic /model auto routing for Hermes. "
    "Run `setup` first to install both home-scoped Hermes components and the "
    "literal auto alias."
)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``hermes-auto`` parser.

    Also used by the control plugin: Hermes's ``register_cli_command`` hands a
    plugin a subparser to populate, and :func:`add_subcommands` fills either one
    identically so ``hermes auto status`` and ``hermes-auto status`` cannot
    diverge.
    """
    parser = argparse.ArgumentParser(prog="hermes-auto", description=_DESCRIPTION)
    parser.add_argument("--version", action="version", version=__version__)
    add_subcommands(parser)
    return parser


def add_subcommands(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach the focused product subcommands to *parser*."""
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    setup = subparsers.add_parser(
        "setup",
        help="install both Hermes shims, tokens, control plugin, and Auto alias",
        description=(
            "Installs the provider and dependency-free control shims into "
            "$HERMES_HOME, enables the control plugin, and configures literal "
            "`/model auto`."
        ),
    )
    setup.add_argument(
        "--hermes-home",
        default=None,
        help="override $HERMES_HOME (default: the location Hermes itself uses)",
    )
    setup.set_defaults(func=lambda args: commands.cmd_setup(hermes_home=args.hermes_home))

    configure = subparsers.add_parser(
        "configure",
        help="discover suggestions and approve the Auto candidate shortlist",
    )
    configure.add_argument("--hermes-home", default=None, help="override $HERMES_HOME")
    configure.set_defaults(
        func=lambda args: commands.cmd_configure(hermes_home=args.hermes_home)
    )

    explain = subparsers.add_parser(
        "explain",
        help="explain the latest or a named session's in-memory route",
    )
    explain.add_argument("--session-id", default="latest")
    explain.set_defaults(
        func=lambda args: commands.cmd_explain(session_id=args.session_id)
    )

    overview = subparsers.add_parser(
        "overview",
        help="show gateway health, candidate count, and the latest decision",
    )
    overview.set_defaults(func=lambda args: commands.cmd_overview())

    start = subparsers.add_parser("start", help="start the gateway sidecar")
    start.set_defaults(func=lambda args: commands.cmd_start())

    stop = subparsers.add_parser("stop", help="stop the gateway sidecar")
    stop.add_argument(
        "--timeout",
        type=float,
        default=commands.supervisor.DEFAULT_STOP_TIMEOUT_SECONDS,
        help="seconds to allow for a graceful drain before escalating",
    )
    stop.set_defaults(func=lambda args: commands.cmd_stop(timeout=args.timeout))

    restart = subparsers.add_parser(
        "restart",
        help="restart the sidecar, waiting for the instance_id to change",
    )
    restart.set_defaults(func=lambda args: commands.cmd_restart())

    status = subparsers.add_parser("status", help="report whether the gateway is running")
    status.set_defaults(func=lambda args: commands.cmd_status())

    doctor = subparsers.add_parser(
        "doctor",
        help="run every diagnostic and report all of them",
    )
    doctor.add_argument("--hermes-home", default=None, help="override $HERMES_HOME")
    doctor.set_defaults(func=lambda args: commands.cmd_doctor(hermes_home=args.hermes_home))

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse *argv* and run the selected command. Returns an exit code.

    Never raises for a command failure. A traceback out of a CLI tells the user
    nothing they can act on and hides the message that would have.
    """
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    handler = getattr(args, "func", None)
    if handler is None:
        # No subcommand. Help plus a non-zero code, so a script that forgot the
        # verb fails rather than silently succeeding.
        parser.print_help()
        return 2

    try:
        return int(handler(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
