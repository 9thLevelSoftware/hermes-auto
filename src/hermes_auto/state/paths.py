"""Per-OS resolution of the router state directory and the files inside it.

This module is the single place that answers "where does the sidecar keep its
state?". Three properties are load-bearing and must survive every future edit:

1. **Standard library only.** The provider shim delivered by plan 02-05 executes
   *inside the Hermes virtual environment*, on the far side of the dependency
   isolation boundary (02-CONTEXT § Phase-Wide Constraints item 1). It must be
   able to import this module to find the token and the runtime file without
   dragging the sidecar's dependency tree across that boundary. Importing
   anything third-party here -- including ``yaml`` -- breaks the shim.

2. **No import from other ``hermes_auto`` subpackages.** Same reason, plus it
   keeps the import graph one-directional: ``config`` and ``state.runtime`` and
   ``gateway.auth`` all import *this*, and this imports none of them.

3. **Directory modes are a POSIX-only guarantee, and this module says so.**
   ``os.chmod`` does not touch Windows ACLs. Measured on the development machine:
   after ``os.chmod(p, 0o600)``, ``p.stat().st_mode & 0o777`` is still ``0o666``.
   So the ``0o700`` applied below is a real access control on POSIX and a no-op
   on Windows. Restrictive permissions for the *token file* on Windows are
   therefore handled with ``icacls`` in ``hermes_auto.gateway.auth``, which reads
   the effective ACL back rather than assuming a write succeeded. Do not "fix"
   this module by adding a ``chmod`` for Windows -- it would create the exact
   false sense of protection this docstring exists to prevent.

Default location is ``~/.hermes/auto-router`` per design.md 16
(``auto_router.gateway.state_dir``).
"""

from __future__ import annotations

import os
import pathlib

# design.md 16: auto_router.gateway.state_dir: ~/.hermes/auto-router
DEFAULT_STATE_DIR: str = "~/.hermes/auto-router"

# Test and operator override. Takes precedence over the configured value so a
# test run can never touch a developer's real state directory, and so a broken
# config.yaml can still be worked around without editing it.
STATE_DIR_ENV_VAR: str = "HERMES_AUTO_STATE_DIR"

# Owner-only. Enforced on POSIX; see module docstring for why this is a no-op on
# Windows and what stands in for it there.
DIR_MODE: int = 0o700

RUNTIME_DIR_NAME: str = "runtime"
LOG_DIR_NAME: str = "logs"
TOKEN_FILE_NAME: str = "token"


def _expand(value: str | os.PathLike[str]) -> pathlib.Path:
    """Expand ``~`` and environment variables, then make the path absolute."""
    text = os.path.expandvars(os.fspath(value))
    return pathlib.Path(text).expanduser().resolve()


def _ensure_dir(path: pathlib.Path) -> pathlib.Path:
    """Create ``path`` if absent, owner-only on POSIX.

    The explicit ``chmod`` after ``mkdir`` is not redundant: ``mkdir``'s ``mode``
    argument is masked by the process umask, so a umask of ``0o022`` would leave
    a "0o700" directory at ``0o700 & ~0o022 == 0o700``, but a umask of ``0o077``
    on a *pre-existing* directory would leave whatever mode it already had. The
    chmod is applied only to directories this call created, so an operator who
    deliberately widened an existing state directory is not overridden silently.
    """
    already_existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    if not already_existed and os.name == "posix":
        os.chmod(path, DIR_MODE)
    return path


def state_dir(
    configured: str | os.PathLike[str] | None = None,
    *,
    create: bool = True,
) -> pathlib.Path:
    """Resolve the router state directory.

    Precedence, highest first:

    1. ``HERMES_AUTO_STATE_DIR`` -- the override hook. It wins over config so a
       test process is structurally incapable of writing into a real install.
    2. ``configured`` -- the value of ``gateway.state_dir`` from config.yaml,
       passed in by the caller. This module never reads config itself; see the
       stdlib-only constraint in the module docstring.
    3. ``~/.hermes/auto-router`` -- the design.md 16 default.

    ``create=False`` resolves the path without touching the filesystem, which is
    what read paths (``read_runtime``, ``read_token``) want: asking "is there a
    running gateway?" must not have the side effect of creating an install.
    """
    override = os.environ.get(STATE_DIR_ENV_VAR)
    if override:
        resolved = _expand(override)
    elif configured is not None and str(configured).strip():
        resolved = _expand(configured)
    else:
        resolved = _expand(DEFAULT_STATE_DIR)

    return _ensure_dir(resolved) if create else resolved


def runtime_dir(
    configured: str | os.PathLike[str] | None = None,
    *,
    create: bool = True,
) -> pathlib.Path:
    """Directory holding the runtime file (``<state_dir>/runtime``)."""
    parent = state_dir(configured, create=create)
    target = parent / RUNTIME_DIR_NAME
    return _ensure_dir(target) if create else target


def log_dir(
    configured: str | os.PathLike[str] | None = None,
    *,
    create: bool = True,
) -> pathlib.Path:
    """Directory for the sidecar's rotating stdout/stderr log."""
    parent = state_dir(configured, create=create)
    target = parent / LOG_DIR_NAME
    return _ensure_dir(target) if create else target


def token_path(
    configured: str | os.PathLike[str] | None = None,
    *,
    create: bool = True,
) -> pathlib.Path:
    """Path to the generated bearer-token file (``<state_dir>/token``).

    ``create`` refers to the *containing directory*, never to the token file
    itself. Creating the token file is ``gateway.auth.mint_token``'s job, because
    only that module knows how to create it with restrictive permissions from the
    first byte.
    """
    return state_dir(configured, create=create) / TOKEN_FILE_NAME


__all__ = [
    "DEFAULT_STATE_DIR",
    "STATE_DIR_ENV_VAR",
    "DIR_MODE",
    "RUNTIME_DIR_NAME",
    "LOG_DIR_NAME",
    "TOKEN_FILE_NAME",
    "state_dir",
    "runtime_dir",
    "log_dir",
    "token_path",
]
