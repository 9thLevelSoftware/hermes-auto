"""Install the provider shim into the directory Hermes actually scans.

``providers/__init__.py`` ``_discover_providers`` imports every
``<dir>/__init__.py`` under ``$HERMES_HOME/plugins/model-providers/``, after the
bundled plugins and with last-writer-wins on name collision. That scan is the
only registration path for a model provider; the ``hermes_agent.plugins``
entry-point group covers general plugins and is not consulted here. So
``pip install hermes-auto-router`` registers nothing, and this module -- reached
by ``hermes auto setup`` -- is what makes the provider exist.

Ownership is tracked by a marker line rather than by a manifest, because the
target is a plain Python file inside the user's Hermes home and a user or
another tool may legitimately have put something there first.
:data:`~hermes_auto.hermes_shim.template.MARKER` on the first line means this
project wrote the file and may replace or remove it. Anything else means it did
not, and neither operation proceeds.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

from hermes_auto.hermes_shim.control_template import (
    CONTROL_MANIFEST,
    CONTROL_MANIFEST_MARKER,
    CONTROL_MARKER,
    CONTROL_SOURCE,
    render_control,
    render_manifest,
)
from hermes_auto.hermes_shim.template import MARKER, SHIM_SOURCE, render
from hermes_auto.provider import PROVIDER_NAME, TOKEN_ENV_VAR, gateway_base_url
from hermes_auto.version import __version__

__all__ = [
    "InstallError",
    "default_hermes_home",
    "control_installed_paths",
    "install",
    "install_control",
    "installed_path",
    "uninstall",
]

_PLUGIN_SUBPATH = ("plugins", "model-providers", PROVIDER_NAME)
_ENTRY_FILENAME = "__init__.py"
_CONTROL_SUBPATH = ("plugins", "hermes-auto-control")
_CONTROL_MANIFEST_FILENAME = "plugin.yaml"

# Names tolerated inside the plugin directory when removing it on uninstall.
# Hermes imports the shim, so CPython leaves a __pycache__ behind that this
# project caused to exist; anything else is somebody else's and the directory
# stays.
_BYTECODE_SUFFIXES = (".pyc", ".pyo")


class InstallError(Exception):
    """The shim could not be installed or removed.

    Raised when ``$HERMES_HOME`` cannot be resolved, when the target path is
    occupied by a file this project did not write, and when the filesystem
    refuses the write or the removal. Never raised merely because nothing is
    installed -- :func:`uninstall` reports that by returning ``False``.
    """


def default_hermes_home() -> pathlib.Path:
    """Resolve ``$HERMES_HOME`` the way Hermes itself does.

    Mirrors ``hermes_constants._get_platform_default_hermes_home`` and
    ``_hermes_home_from_env``: the ``HERMES_HOME`` environment variable, else
    ``%LOCALAPPDATA%/hermes`` (falling back to ``~/AppData/Local/hermes``) on
    Windows, else ``~/.hermes``.

    Reimplemented rather than imported on purpose. This runs in the router's
    virtualenv, where Hermes is not importable -- it is a source checkout, not
    an installed distribution -- so importing ``hermes_constants`` here would
    fail in exactly the environment ``hermes auto setup`` runs in. The context
    override that Hermes layers on top (``set_hermes_home_override``) is
    process-local to Hermes and cannot apply to this one.

    Raises:
        InstallError: if no home directory can be determined at all.
    """
    from_env = os.environ.get("HERMES_HOME", "").strip()
    if from_env:
        return pathlib.Path(from_env)

    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            return pathlib.Path(local_appdata) / "hermes"
        return _home() / "AppData" / "Local" / "hermes"

    return _home() / ".hermes"


def _home() -> pathlib.Path:
    try:
        return pathlib.Path.home()
    except (RuntimeError, OSError) as exc:  # no HOME/USERPROFILE at all
        raise InstallError(
            "cannot determine the Hermes home directory: neither HERMES_HOME "
            f"nor a user home directory is resolvable ({exc})"
        ) from exc


def _resolve_home(hermes_home: str | os.PathLike[str] | None) -> pathlib.Path:
    if hermes_home is None:
        return default_hermes_home()
    return pathlib.Path(hermes_home)


def installed_path(
    hermes_home: str | os.PathLike[str] | None = None,
) -> pathlib.Path:
    """Return the file Hermes will import to register this provider.

    ``<hermes_home>/plugins/model-providers/hermes-auto/__init__.py``. The
    directory name is the plugin id; the file name is what the scan looks for.
    Computed, never created -- asking where the shim goes must not have the
    side effect of putting it there.
    """
    return _resolve_home(hermes_home).joinpath(*_PLUGIN_SUBPATH, _ENTRY_FILENAME)


def control_installed_paths(
    hermes_home: str | os.PathLike[str] | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    directory = _resolve_home(hermes_home).joinpath(*_CONTROL_SUBPATH)
    return directory / _ENTRY_FILENAME, directory / _CONTROL_MANIFEST_FILENAME


def _read_existing(target: pathlib.Path) -> str | None:
    """Return the target's text, or ``None`` if it does not exist."""
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        # Unreadable or not UTF-8 text: it is certainly not a file this
        # project wrote, and it must not be replaced on a guess.
        raise InstallError(
            f"refusing to touch {target}: it exists but could not be read "
            f"to check ownership ({exc})"
        ) from exc


def _is_owned(content: str) -> bool:
    return content.startswith(MARKER)


def install(
    hermes_home: str | os.PathLike[str] | None = None,
    *,
    base_url: str | None = None,
    token_env_var: str = TOKEN_ENV_VAR,
    plugin_version: str = __version__,
) -> pathlib.Path:
    """Write the shim into ``$HERMES_HOME`` and return the path written.

    Idempotent: running it again over a file this project owns is an upgrade
    and overwrites cleanly. Running it over a file this project does not own
    raises rather than clobbering.

    The write is atomic -- a temporary file in the destination directory
    followed by ``os.replace`` -- so a discovery scan racing the installer sees
    either the previous shim or the new one, never a half-written module that
    would fail to import and drop the provider from the registry.

    Args:
        hermes_home: Override for ``$HERMES_HOME``. Defaults to
            :func:`default_hermes_home`.
        base_url: The gateway address to bake into the shim. Defaults to the
            configured ``auto_router.gateway.url`` with ``/v1`` appended, since
            the shim cannot read configuration at run time.
        token_env_var: Name of the environment variable holding the gateway
            bearer token. Only the name is written; the value never is.
        plugin_version: Value reported as ``plugin_version`` in the envelope.

    Raises:
        InstallError: if ``$HERMES_HOME`` is unresolvable, the target is
            occupied by an unowned file, or the filesystem refuses the write.
    """
    target = installed_path(hermes_home)

    existing = _read_existing(target)
    if existing is not None and not _is_owned(existing):
        raise InstallError(
            f"refusing to overwrite {target}: it exists and does not begin "
            f"with this project's marker line, so it was not written by "
            f"hermes-auto-router. Remove or rename it first."
        )

    # ``gateway_base_url`` raises ``ConfigError`` on a present-but-broken
    # configuration file, and that is left to propagate. Baking a fallback
    # endpoint into a file that then persists on disk would send traffic
    # somewhere the operator did not choose, and would keep doing so after the
    # configuration was fixed.
    resolved_base_url = base_url if base_url is not None else gateway_base_url()
    source = render(
        SHIM_SOURCE,
        base_url=resolved_base_url,
        token_env_var=token_env_var,
        plugin_version=plugin_version,
    )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, source)
    except OSError as exc:
        raise InstallError(f"could not write the provider shim to {target}: {exc}") from exc

    return target


def _atomic_write(target: pathlib.Path, source: str) -> None:
    """Write *source* to *target* via a temporary file in the same directory.

    Same directory so ``os.replace`` stays within one filesystem, where it is
    atomic on POSIX and on Windows.
    """
    handle, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".shim-", suffix=".tmp"
    )
    temp_path = pathlib.Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _control_executable(
    executable: str | os.PathLike[str] | None,
) -> pathlib.Path:
    if executable is not None:
        resolved = pathlib.Path(executable).expanduser().resolve()
    else:
        found = shutil.which("hermes-auto")
        if found:
            resolved = pathlib.Path(found).resolve()
        else:
            name = "hermes-auto.exe" if os.name == "nt" else "hermes-auto"
            resolved = pathlib.Path(sys.executable).with_name(name).resolve()
    if not resolved.is_file():
        raise InstallError(
            "cannot install the control plugin: the absolute hermes-auto "
            f"executable was not found at {resolved}"
        )
    return resolved


def install_control(
    hermes_home: str | os.PathLike[str] | None = None,
    *,
    executable: str | os.PathLike[str] | None = None,
    plugin_version: str = __version__,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Atomically upgrade the dependency-free home-scoped control plugin."""
    init_path, manifest_path = control_installed_paths(hermes_home)
    targets = (
        (init_path, CONTROL_MARKER),
        (manifest_path, CONTROL_MANIFEST_MARKER),
    )
    previous: dict[pathlib.Path, str | None] = {}
    for target, marker in targets:
        content = _read_existing(target)
        if content is not None and not content.startswith(marker):
            raise InstallError(
                f"refusing to overwrite {target}: it is not managed by "
                "hermes-auto-router"
            )
        previous[target] = content

    resolved = _control_executable(executable)
    rendered = {
        init_path: render_control(CONTROL_SOURCE, executable=str(resolved)),
        manifest_path: render_manifest(
            CONTROL_MANIFEST, plugin_version=plugin_version
        ),
    }
    try:
        init_path.parent.mkdir(parents=True, exist_ok=True)
        for target, source in rendered.items():
            _atomic_write(target, source)
    except BaseException as exc:
        for target, content in previous.items():
            try:
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target, content)
            except OSError:
                pass
        if isinstance(exc, InstallError):
            raise
        raise InstallError(f"could not install the control plugin: {exc}") from exc
    return init_path, manifest_path


def uninstall(hermes_home: str | os.PathLike[str] | None = None) -> bool:
    """Remove the installed shim. Returns whether anything was removed.

    ``False`` means nothing was installed -- an absent install is not an error,
    so ``uninstall`` is safe to call unconditionally and safe to call twice.

    Raises:
        InstallError: if a file occupies the target path but does not carry
            this project's marker. Deleting it would be the same clobbering
            :func:`install` refuses, so it is refused here too rather than
            reported as "nothing was installed".
    """
    target = installed_path(hermes_home)

    existing = _read_existing(target)
    if existing is None:
        return False
    if not _is_owned(existing):
        raise InstallError(
            f"refusing to remove {target}: it does not begin with this "
            f"project's marker line, so it was not written by "
            f"hermes-auto-router."
        )

    try:
        target.unlink()
    except OSError as exc:
        raise InstallError(f"could not remove the provider shim at {target}: {exc}") from exc

    _remove_plugin_dir_if_ours(target.parent)
    return True


def _remove_plugin_dir_if_ours(plugin_dir: pathlib.Path) -> None:
    """Best-effort removal of the now-empty plugin directory.

    Hermes imports the shim, so CPython leaves a ``__pycache__`` holding the
    compiled copy. That directory exists because of this project and is removed
    with the plugin -- but only when it contains bytecode and nothing else.
    Anything unexpected in the plugin directory means somebody else is using
    it, and it stays.

    Failure is not raised: the provider is already unregistered once
    ``__init__.py`` is gone, because the discovery scan skips a directory
    without one. A leftover empty directory is untidy, not broken.
    """
    try:
        for child in list(plugin_dir.iterdir()):
            if child.name == "__pycache__" and child.is_dir():
                if all(
                    item.is_file() and item.suffix in _BYTECODE_SUFFIXES
                    for item in child.iterdir()
                ):
                    for item in child.iterdir():
                        item.unlink()
                    child.rmdir()
                continue
            return  # something else lives here; leave the directory alone
        plugin_dir.rmdir()
    except OSError:
        return
