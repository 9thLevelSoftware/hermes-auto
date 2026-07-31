"""Dependency-free home-scoped Hermes control plugin template."""

from __future__ import annotations

CONTROL_MARKER = (
    "# hermes-auto-router control shim v1 -- managed file, do not edit"
)
CONTROL_MANIFEST_MARKER = (
    "# hermes-auto-router control manifest v1 -- managed file, do not edit"
)

CONTROL_SOURCE = CONTROL_MARKER + r'''
#
# This module runs in Hermes's virtual environment. It deliberately imports
# only the standard library and delegates every operation to the absolute
# hermes-auto executable installed with hermes-auto-router.

import os
import shlex
import subprocess

HERMES_AUTO_EXECUTABLE = "__HERMES_AUTO_EXECUTABLE__"


def _run(arguments, *, capture):
    command = [HERMES_AUTO_EXECUTABLE, *arguments]
    try:
        completed = subprocess.run(
            command,
            capture_output=capture,
            text=True,
            check=False,
        )
    except OSError as exc:
        return 1, "hermes-auto could not be started (" + type(exc).__name__ + ")"
    if not capture:
        return completed.returncode, ""
    output = (completed.stdout or "").strip()
    if not output:
        output = (completed.stderr or "").strip()
    return completed.returncode, output


def _slash_auto(raw_args=""):
    try:
        arguments = shlex.split(raw_args or "", posix=(os.name != "nt"))
    except ValueError:
        return "Invalid /auto arguments."
    if not arguments:
        arguments = ["overview"]
    _code, output = _run(arguments, capture=True)
    return output or "hermes-auto returned no output."


def _setup_cli(parser):
    parser.add_argument(
        "hermes_auto_arguments",
        nargs="...",
        help="arguments forwarded to the hermes-auto executable",
    )


def _handle_cli(args):
    arguments = list(getattr(args, "hermes_auto_arguments", ()) or ())
    code, _output = _run(arguments, capture=False)
    return code


def _on_session_start(**_kwargs):
    _run(["start"], capture=True)


def register(ctx):
    ctx.register_cli_command(
        "auto",
        help="Configure and control /model auto",
        setup_fn=_setup_cli,
        handler_fn=_handle_cli,
        description="Configure candidates, supervise the gateway, and explain routes.",
    )
    ctx.register_command(
        "auto",
        _slash_auto,
        description="Show Auto health or explain the most recent route",
        args_hint="[status|explain]",
    )
    ctx.register_hook("on_session_start", _on_session_start)
'''

CONTROL_MANIFEST = CONTROL_MANIFEST_MARKER + """
name: hermes-auto-control
version: __HERMES_AUTO_PLUGIN_VERSION__
description: "Dependency-free control surface for /model auto"
hooks:
  - on_session_start
"""


def render_control(source: str, *, executable: str) -> str:
    placeholder = '"__HERMES_AUTO_EXECUTABLE__"'
    if placeholder not in source:
        raise ValueError("control template is missing its executable placeholder")
    return source.replace(placeholder, repr(str(executable)), 1)


def render_manifest(source: str, *, plugin_version: str) -> str:
    placeholder = "__HERMES_AUTO_PLUGIN_VERSION__"
    if placeholder not in source:
        raise ValueError("control manifest is missing its version placeholder")
    return source.replace(placeholder, str(plugin_version), 1)


__all__ = [
    "CONTROL_MANIFEST",
    "CONTROL_MANIFEST_MARKER",
    "CONTROL_MARKER",
    "CONTROL_SOURCE",
    "render_control",
    "render_manifest",
]
