"""The provider shim that this project installs into the Hermes environment.

Hermes discovers model providers by scanning
``$HERMES_HOME/plugins/model-providers/<name>/`` and importing each directory's
``__init__.py`` (``providers/__init__.py`` ``_discover_providers``). It does not
consult pip entry points for model providers -- the ``hermes_agent.plugins``
entry-point group exists, but only for general plugins. Installing this
distribution with pip therefore registers nothing, and a user who does only
that gets "unknown provider hermes-auto" at first use.

This package closes that gap. :mod:`~hermes_auto.hermes_shim.template` holds the
source of the module Hermes will import, and
:mod:`~hermes_auto.hermes_shim.installer` writes it into the scanned directory.

The written file executes inside the *Hermes* interpreter, where this project's
dependencies do not exist. It imports the standard library and Hermes's own
``providers`` package only, and an AST scan over the template enforces that --
the isolation constraint is structural here rather than remembered.
"""

from hermes_auto.hermes_shim.installer import (
    InstallError,
    install,
    installed_path,
    uninstall,
)
from hermes_auto.hermes_shim.template import MARKER, SHIM_SOURCE, render

__all__ = [
    "MARKER",
    "SHIM_SOURCE",
    "InstallError",
    "install",
    "installed_path",
    "render",
    "uninstall",
]
