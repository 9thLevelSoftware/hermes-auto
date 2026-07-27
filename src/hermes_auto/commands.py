"""Implementations behind ``hermes auto ...`` and ``hermes-auto ...``.

These live apart from :mod:`hermes_auto.cli` for one concrete reason: the Hermes
control plugin needs to run them, and it has no argv to parse. A command whose
body is welded to an ``argparse.Namespace`` can only be invoked by
reconstructing a fake namespace, which is how a CLI and a plugin drift into
two behaviours with one name. Everything here takes ordinary keyword arguments
and returns an exit code.

**The bootstrap problem ``setup`` exists to solve.** Two independent Hermes
mechanisms have to be satisfied before any of this works, and neither is
implied by ``pip install``:

1. **Model providers are discovered by directory scan**, not by entry point
   (02-CONTEXT § VERIFIED HERMES FACTS item 1). Nothing about installing this
   distribution puts a provider in front of Hermes. ``setup`` writes the shim
   into ``$HERMES_HOME/plugins/model-providers/hermes-auto/__init__.py``, and
   that write *is* the registration. A user who skips it sees "unknown provider
   hermes-auto" with nothing to connect it to.
2. **Entry-point plugins are opt-in via ``plugins.enabled``**
   (``hermes_cli/plugins.py:1453``). When the key is absent
   ``_get_enabled_plugins()`` returns ``None``, which means *nothing* is
   enabled -- the plugin is recorded with ``error = "not enabled in config"``
   and its ``register()`` is never called.

Point 2 is circular on a fresh install: the command that would enable the
plugin is registered *by* the plugin. The console script in
``[project.scripts]`` is the way out, and it is why that entry exists. On a
first install the user runs ``hermes-auto setup`` (the script); from then on
``hermes auto ...`` (the plugin) works too. ``setup`` prints this ordering
rather than leaving the user to infer it from a failure.

**``doctor`` never stops at the first failure.** A diagnostic that aborts on
check one hides checks two through nine, and the hidden ones are usually the
explanation. Every check runs, every result is printed, and the exit code is
derived at the end.

**``doctor`` reads permissions back rather than trusting a write.**
``os.chmod`` is a no-op on Windows ACLs, so a ``0o600`` that "succeeded" can
still leave the token readable by other accounts on the machine. The authority
is ``token_permissions_ok()``, which re-reads the effective ACL.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import pathlib
import tempfile
import urllib.error
import urllib.request
from typing import Any

from . import supervisor
from .compatibility import CompatibilityStatus, check_compatibility
from .config import AutoRouterConfig, ConfigError, load_config
from .provider import PROVIDER_NAME, TOKEN_ENV_VAR
from .version import __version__

__all__ = [
    "CONTROL_PLUGIN_NAME",
    "Check",
    "LEVEL_FAIL",
    "LEVEL_OK",
    "LEVEL_WARN",
    "cmd_doctor",
    "cmd_restart",
    "cmd_setup",
    "cmd_start",
    "cmd_status",
    "cmd_stop",
    "control_plugin_enabled",
    "enable_control_plugin",
    "hermes_config_path",
    "installed_shim_version",
    "run_doctor",
]

#: The name Hermes knows this plugin by. It is the ``[project.entry-points]``
#: key, because ``_scan_entry_points`` builds the manifest with
#: ``name=ep.name``, and ``plugins.enabled`` is matched against exactly that.
#: Changing one without the other silently disables the plugin.
CONTROL_PLUGIN_NAME = "hermes-auto-control"

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_FAIL = "fail"

_MARKS = {LEVEL_OK: "  ok  ", LEVEL_WARN: " warn ", LEVEL_FAIL: " FAIL "}


@dataclasses.dataclass(frozen=True)
class Check:
    """One ``doctor`` result.

    Structured rather than a printed line so tests can assert on outcomes
    without parsing prose, and so a future ``--json`` mode costs nothing.
    """

    name: str
    level: str
    detail: str


# ---------------------------------------------------------------------------
# Hermes-side configuration: enabling the control plugin
# ---------------------------------------------------------------------------


def hermes_config_path(hermes_home: str | os.PathLike[str] | None = None) -> pathlib.Path:
    """``$HERMES_HOME/config.yaml`` -- the file ``plugins.enabled`` lives in."""
    from .hermes_shim.installer import default_hermes_home

    base = pathlib.Path(hermes_home) if hermes_home is not None else default_hermes_home()
    return base / "config.yaml"


def _load_yaml(path: pathlib.Path) -> tuple[dict[str, Any] | None, str, str]:
    """Return ``(document, text, error)`` for Hermes's config file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "", f"{path} does not exist"
    except OSError as exc:
        return None, "", f"{path} could not be read ({exc})"

    import yaml

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, text, f"{path} is not valid YAML ({exc})"
    if document is None:
        document = {}
    if not isinstance(document, dict):
        return None, text, f"{path} does not contain a YAML mapping at the top level"
    return document, text, ""


def _enabled_list(document: dict[str, Any]) -> list[Any] | None:
    plugins = document.get("plugins")
    if not isinstance(plugins, dict):
        return None
    enabled = plugins.get("enabled")
    return enabled if isinstance(enabled, list) else None


def control_plugin_enabled(
    hermes_home: str | os.PathLike[str] | None = None,
) -> tuple[bool, str]:
    """Is ``hermes-auto-control`` in Hermes's ``plugins.enabled`` allow-list?"""
    path = hermes_config_path(hermes_home)
    document, _, error = _load_yaml(path)
    if document is None:
        return False, error
    enabled = _enabled_list(document)
    if enabled is None:
        return False, (
            f"{path} has no plugins.enabled list. Hermes treats an absent key as "
            f"'nothing enabled', so the control plugin's register() is never "
            f"called and `hermes auto ...` does not exist."
        )
    if CONTROL_PLUGIN_NAME in enabled:
        return True, f"{CONTROL_PLUGIN_NAME} is listed in {path} plugins.enabled"
    return False, (
        f"{CONTROL_PLUGIN_NAME} is not in {path} plugins.enabled "
        f"(found: {', '.join(str(item) for item in enabled) or 'nothing'})"
    )


_MANUAL_INSTRUCTION = (
    f"run `hermes plugins enable {CONTROL_PLUGIN_NAME}`, or add "
    f"`{CONTROL_PLUGIN_NAME}` to the plugins.enabled list in your Hermes "
    f"config.yaml by hand"
)

_ADDED_COMMENT = "# Added by `hermes-auto setup` (hermes-auto-router).\n"


def _plan_edit(text: str, document: dict[str, Any]) -> str | None:
    """Return the edited YAML text, or ``None`` when it cannot be done safely.

    Text surgery rather than a parse/re-dump round trip: ``yaml.safe_dump`` of a
    parsed document discards every comment and reflows the whole file, and this
    is the user's Hermes configuration, not ours. Anything that is not an
    unambiguous block-style structure returns ``None`` so the caller falls back
    to telling the user the exact command to run. Refusing to edit is a safe
    outcome; guessing at someone's YAML is not.
    """
    lines = text.splitlines(keepends=True)
    item = f"- {CONTROL_PLUGIN_NAME}\n"

    if "plugins" not in document:
        prefix = text if (not text or text.endswith("\n")) else text + "\n"
        return f"{prefix}\n{_ADDED_COMMENT}plugins:\n  enabled:\n    {item}"

    if not isinstance(document.get("plugins"), dict):
        # `plugins:` is a scalar or a list. Not our shape; do not reinterpret it.
        return None

    plugins_index = None
    for index, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if stripped.startswith("plugins:") and stripped[len("plugins:") :].strip() == "":
            plugins_index = index
            break
    if plugins_index is None:
        # A flow mapping (`plugins: {enabled: [...]}`) or an anchor/merge key.
        return None

    enabled = _enabled_list(document)
    if enabled is None:
        return "".join(
            lines[: plugins_index + 1]
            + [f"  enabled:\n    {item}"]
            + lines[plugins_index + 1 :]
        )

    enabled_index = None
    enabled_indent = ""
    for index in range(plugins_index + 1, len(lines)):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            break  # left the plugins: block
        if stripped.startswith("enabled:"):
            if stripped[len("enabled:") :].strip():
                # `enabled: []` or an inline list. Editing this correctly means
                # reimplementing flow-sequence syntax; refuse instead.
                return None
            enabled_index = index
            enabled_indent = " " * indent
            break
    if enabled_index is None:
        return None

    insert_at = enabled_index + 1
    item_indent = enabled_indent + "  "
    for index in range(enabled_index + 1, len(lines)):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent <= len(enabled_indent):
            break
        if stripped.startswith("- "):
            item_indent = " " * indent
            insert_at = index + 1
    return "".join(lines[:insert_at] + [f"{item_indent}{item}"] + lines[insert_at:])


def _atomic_write_text(target: pathlib.Path, text: str) -> None:
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".hermes-auto-", suffix=".tmp")
    temp_path = pathlib.Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def enable_control_plugin(
    hermes_home: str | os.PathLike[str] | None = None,
) -> tuple[bool, str]:
    """Add ``hermes-auto-control`` to Hermes's ``plugins.enabled``.

    Returns ``(changed_or_already_enabled, detail)``. ``False`` means the user
    must run the command named in *detail* -- which is a supported outcome, not
    a crash, because this edits a file this project does not own.

    Three safeguards, because that file is the user's Hermes configuration:
    a timestamped backup beside it, an atomic replace, and a **re-parse of the
    result** which must equal the original document plus exactly this one name.
    If the verification fails the backup is restored, so a bug in the text
    surgery above degrades to "tell the user the command" rather than to a
    corrupted config.
    """
    path = hermes_config_path(hermes_home)
    document, text, error = _load_yaml(path)
    if document is None:
        return False, f"{error}; {_MANUAL_INSTRUCTION}"

    already, detail = control_plugin_enabled(hermes_home)
    if already:
        return True, detail

    edited = _plan_edit(text, document)
    if edited is None:
        return False, (
            f"{path} has a plugins section this command will not rewrite "
            f"automatically; {_MANUAL_INSTRUCTION}"
        )

    backup = path.with_name(f"{path.name}.hermes-auto.bak")
    try:
        if path.exists():
            backup.write_text(text, encoding="utf-8", newline="")
        _atomic_write_text(path, edited)
    except OSError as exc:
        return False, f"{path} could not be updated ({exc}); {_MANUAL_INSTRUCTION}"

    verified, verify_detail = control_plugin_enabled(hermes_home)
    new_document, _, new_error = _load_yaml(path)
    expected = json.loads(json.dumps(document, default=str))
    if new_document is not None:
        observed = json.loads(json.dumps(new_document, default=str))
        observed_plugins = observed.get("plugins")
        if isinstance(observed_plugins, dict):
            remaining = dict(observed_plugins)
            listed = remaining.get("enabled")
            if isinstance(listed, list) and CONTROL_PLUGIN_NAME in listed:
                trimmed = [x for x in listed if x != CONTROL_PLUGIN_NAME]
                if trimmed:
                    remaining["enabled"] = trimmed
                else:
                    remaining.pop("enabled", None)
            if remaining:
                observed["plugins"] = remaining
            else:
                observed.pop("plugins", None)
        unchanged = observed == expected
    else:
        unchanged = False

    if not verified or not unchanged:
        with contextlib.suppress(OSError):
            _atomic_write_text(path, text)
        return False, (
            f"{path} was left unchanged: the edit did not verify "
            f"({new_error or verify_detail}); {_MANUAL_INSTRUCTION}"
        )

    return True, (
        f"added {CONTROL_PLUGIN_NAME} to plugins.enabled in {path} "
        f"(previous contents saved as {backup.name})"
    )


# ---------------------------------------------------------------------------
# Shim inspection
# ---------------------------------------------------------------------------


def installed_shim_version(text: str) -> str | None:
    """Extract ``PLUGIN_VERSION`` from an installed shim's source.

    Parsed from the AST rather than imported: the shim ``import``s ``providers``
    and calls ``register_provider`` at module scope, neither of which exists in
    this virtualenv. Parsed rather than regexed because the value is a rendered
    ``repr()`` and may legitimately be quoted either way.
    """
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PLUGIN_VERSION":
                if isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str
                ):
                    return node.value.value
    return None


# ---------------------------------------------------------------------------
# Token helpers shared by setup and doctor
# ---------------------------------------------------------------------------


def _ensure_admin_token(state_dir: Any = None) -> tuple[bool, str]:
    """Mint the admin token when absent. Returns ``(minted, detail)``.

    Safe to do while a gateway is running, and that is not an accident:
    ``gateway/admin.py`` reads the admin token from disk on **every** request,
    so a freshly minted one takes effect immediately (02-06 § Decisions 1).

    The inference token is deliberately *not* treated this way.
    ``gateway/app.py``'s lifespan reads it **once** at startup and holds it in
    ``app.state.token``, so re-minting it under a running gateway would
    invalidate the credential that gateway is still enforcing and lock out every
    caller until a restart.

    Called from both ``setup`` and ``doctor``. ``doctor`` mints too because
    nothing outside the gateway's own lifespan ever did: a gateway started
    before the admin API existed has no admin token, so ``stop`` has no
    credential for ``POST /admin/v1/shutdown`` and silently degrades to a hard
    kill that severs in-flight streams. Minting is reported, never silent.
    """
    from .gateway.admin import mint_admin_token, read_admin_token

    try:
        if read_admin_token(state_dir) is not None:
            return False, "already present"
    except Exception as exc:  # noqa: BLE001 - damaged file: re-mint over it
        del exc
    mint_admin_token(state_dir)
    return True, "minted"


def _http_get(url: str, timeout: float = 3.0) -> tuple[int, bytes] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(64 * 1024)
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_setup(
    *,
    hermes_home: str | os.PathLike[str] | None = None,
    config: AutoRouterConfig | None = None,
    stream: Any = None,
) -> int:
    """Install the provider shim, mint the tokens, and enable the control plugin.

    This is the command that makes the provider exist. ``pip install`` does not
    register anything -- see the module docstring -- and neither does declaring
    the entry point.
    """
    out = _Printer(stream)
    try:
        config = config if config is not None else load_config(None)
    except ConfigError as exc:
        out.line(f"FAILED: configuration is unusable: {exc}")
        return 2

    from .gateway.auth import mint_token, read_token, token_permissions_ok
    from .hermes_shim.installer import InstallError, install

    failures = 0

    out.line("hermes-auto setup")
    out.line("")
    out.line(
        "This step is what registers the provider with Hermes. Hermes discovers "
        "model providers by scanning $HERMES_HOME/plugins/model-providers, so "
        "`pip install hermes-auto-router` alone registers nothing and the model "
        "`auto:balanced` would fail with 'unknown provider hermes-auto'."
    )
    out.line("")

    # --- tokens -----------------------------------------------------------
    try:
        if read_token(config.gateway.state_dir) is None:
            mint_token(config.gateway.state_dir)
            out.line("token          minted the gateway bearer token")
        else:
            out.line("token          already present (left as it is)")
        ok, reason = token_permissions_ok(config.gateway.state_dir)
        out.line(f"token perms    {'ok' if ok else 'PROBLEM'}: {reason}")
        if not ok:
            failures += 1
    except Exception as exc:  # noqa: BLE001 - report, do not abort setup
        out.line(f"token          FAILED: {exc}")
        failures += 1

    try:
        minted, detail = _ensure_admin_token(config.gateway.state_dir)
        out.line(f"admin token    {'minted' if minted else detail}")
        from .gateway.admin import admin_token_permissions_ok

        ok, reason = admin_token_permissions_ok(config.gateway.state_dir)
        out.line(f"admin perms    {'ok' if ok else 'PROBLEM'}: {reason}")
        if not ok:
            failures += 1
    except Exception as exc:  # noqa: BLE001
        out.line(f"admin token    FAILED: {exc}")
        failures += 1

    # --- the provider shim ------------------------------------------------
    try:
        target = install(hermes_home)
        out.line(f"provider shim  installed at {target}")
    except (InstallError, ConfigError) as exc:
        out.line(f"provider shim  FAILED: {exc}")
        failures += 1

    # --- the control plugin ----------------------------------------------
    enabled, detail = enable_control_plugin(hermes_home)
    out.line(f"control plugin {'enabled' if enabled else 'NOT ENABLED'}: {detail}")
    if not enabled:
        failures += 1

    out.line("")
    out.line("Bootstrap ordering, for the next machine:")
    out.line(
        "  1. `hermes-auto setup` -- the console script from [project.scripts]. "
        "It has to be the console script on a first install, because the "
        "`hermes auto` subcommand is registered by the control plugin, and the "
        "control plugin is not enabled until this command enables it."
    )
    out.line(
        f"  2. Export {TOKEN_ENV_VAR} with the contents of the token file so "
        f"Hermes can authenticate to the gateway."
    )
    out.line("  3. `hermes-auto start`, then `hermes-auto doctor`.")
    out.line(
        f"  4. From then on `hermes auto ...` and `/auto status` work inside "
        f"Hermes, and the provider is `{PROVIDER_NAME}`."
    )

    if failures:
        out.line("")
        out.line(f"setup finished with {failures} problem(s); see above.")
        return 1
    return 0


def cmd_start(
    *, config: AutoRouterConfig | None = None, stream: Any = None
) -> int:
    """Start the sidecar, or report that it is already up."""
    out = _Printer(stream)
    try:
        result = supervisor.start(config)
    except supervisor.SupervisorError as exc:
        out.line(f"start failed: {exc}")
        return 1
    out.line(result.detail)
    return 0


def cmd_stop(
    *,
    config: AutoRouterConfig | None = None,
    timeout: float = supervisor.DEFAULT_STOP_TIMEOUT_SECONDS,
    stream: Any = None,
) -> int:
    """Stop the sidecar. Stopping something already stopped is success."""
    out = _Printer(stream)
    try:
        result = supervisor.stop(config, timeout=timeout)
    except supervisor.SupervisorError as exc:
        out.line(f"stop failed: {exc}")
        return 1
    out.line(result.detail)
    return 0


def cmd_restart(
    *, config: AutoRouterConfig | None = None, stream: Any = None
) -> int:
    """Restart the sidecar and confirm the identity actually changed."""
    out = _Printer(stream)
    try:
        result = supervisor.restart(config)
    except supervisor.SupervisorError as exc:
        out.line(f"restart failed: {exc}")
        return 1
    out.line(result.detail)
    return 0


def cmd_status(
    *, config: AutoRouterConfig | None = None, stream: Any = None
) -> int:
    """Report gateway state. Exit 0 whether or not it is running.

    "Not running" is a true answer to the question asked, not a failure of the
    command, and scripting ``hermes auto status`` is much easier when the exit
    code means "I could tell you" rather than "it is up".
    """
    out = _Printer(stream)
    try:
        result = supervisor.status(config)
    except supervisor.SupervisorError as exc:
        out.line(f"status unavailable: {exc}")
        return 1
    out.line(result.detail)
    return 0


def run_doctor(
    *,
    config: AutoRouterConfig | None = None,
    hermes_home: str | os.PathLike[str] | None = None,
) -> list[Check]:
    """Run every diagnostic and return the results. Never raises for a failure.

    Every check is independent and every one runs. The first thing a user does
    with ``doctor`` output is read past the first failure, so aborting there
    would withhold precisely the information they came for.
    """
    checks: list[Check] = []

    # --- configuration ----------------------------------------------------
    resolved: AutoRouterConfig | None = config
    if resolved is None:
        try:
            resolved = load_config(None)
            source = resolved.source_path or "built-in defaults (no config file)"
            checks.append(Check("config", LEVEL_OK, f"loaded from {source}"))
        except ConfigError as exc:
            # ConfigError messages name the offending key by construction
            # (config.py::_fail), which is the whole reason this is reported
            # verbatim rather than summarised.
            checks.append(Check("config", LEVEL_FAIL, str(exc)))
    else:
        checks.append(Check("config", LEVEL_OK, "supplied by the caller"))

    # --- Hermes compatibility --------------------------------------------
    try:
        compatibility = check_compatibility()
        level = {
            CompatibilityStatus.COMPATIBLE: LEVEL_OK,
            # Hermes ships as a source checkout, not an installed distribution
            # (02-CONTEXT § VERIFIED HERMES FACTS item 4), so "not installed" is
            # the normal reading on a developer machine and must not be a
            # failure that trains users to ignore doctor output.
            CompatibilityStatus.HERMES_NOT_INSTALLED: LEVEL_WARN,
            CompatibilityStatus.VERSION_UNREADABLE: LEVEL_WARN,
        }.get(compatibility.status, LEVEL_FAIL)
        checks.append(Check("hermes", level, compatibility.detail))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("hermes", LEVEL_FAIL, f"probe raised {exc!r}"))

    # --- the provider shim ------------------------------------------------
    try:
        from .hermes_shim.installer import installed_path
        from .hermes_shim.template import MARKER, SHIM_SOURCE, render
        from .provider import gateway_base_url

        target = installed_path(hermes_home)
        if not target.exists():
            checks.append(
                Check(
                    "shim",
                    LEVEL_FAIL,
                    f"not installed: {target} does not exist. Hermes discovers "
                    f"providers by directory scan, so until this file exists the "
                    f"provider {PROVIDER_NAME!r} does not exist either. Run "
                    f"`hermes-auto setup`.",
                )
            )
        else:
            text = target.read_text(encoding="utf-8")
            if not text.startswith(MARKER):
                checks.append(
                    Check(
                        "shim",
                        LEVEL_FAIL,
                        f"{target} exists but does not carry this project's "
                        f"marker line, so it was written by something else",
                    )
                )
            else:
                checks.append(Check("shim", LEVEL_OK, f"installed at {target}"))

            on_disk = installed_shim_version(text)
            if on_disk is None:
                checks.append(
                    Check(
                        "shim version",
                        LEVEL_WARN,
                        f"{target} declares no PLUGIN_VERSION; it predates the "
                        f"version marker or was edited",
                    )
                )
            elif on_disk != __version__:
                # Nothing else detects this: the installed file carries the
                # version that wrote it, and an upgrade of this distribution
                # does not rewrite it.
                checks.append(
                    Check(
                        "shim version",
                        LEVEL_FAIL,
                        f"stale shim: {target} was written by version {on_disk}, "
                        f"this is {__version__}. Re-run `hermes-auto setup`.",
                    )
                )
            else:
                checks.append(
                    Check("shim version", LEVEL_OK, f"matches this build ({__version__})")
                )

            try:
                expected = render(SHIM_SOURCE, base_url=gateway_base_url(resolved))
            except ConfigError as exc:
                checks.append(
                    Check("shim drift", LEVEL_WARN, f"not comparable: {exc}")
                )
            else:
                if text == expected:
                    checks.append(
                        Check(
                            "shim drift",
                            LEVEL_OK,
                            "the installed shim is byte-identical to what this "
                            "build would write",
                        )
                    )
                else:
                    checks.append(
                        Check(
                            "shim drift",
                            LEVEL_FAIL,
                            f"the installed shim differs from what this build "
                            f"would write -- its copy of the _hermes_auto "
                            f"envelope may have drifted from provider.py. "
                            f"Re-run `hermes-auto setup`.",
                        )
                    )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("shim", LEVEL_FAIL, f"could not be inspected: {exc!r}"))

    # --- the control plugin ----------------------------------------------
    try:
        enabled, detail = control_plugin_enabled(hermes_home)
        checks.append(
            Check("control plugin", LEVEL_OK if enabled else LEVEL_FAIL, detail)
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("control plugin", LEVEL_FAIL, f"could not be read: {exc!r}"))

    # --- tokens -----------------------------------------------------------
    state_dir = resolved.gateway.state_dir if resolved is not None else None
    try:
        from .gateway.auth import read_token, token_permissions_ok

        if read_token(state_dir) is None:
            checks.append(
                Check("token", LEVEL_FAIL, "no gateway token has been minted; run `hermes-auto setup`")
            )
        else:
            checks.append(Check("token", LEVEL_OK, "present"))
            ok, reason = token_permissions_ok(state_dir)
            # Read back, never assumed: os.chmod does not touch Windows ACLs, so
            # a mint that "succeeded" can still leave the file readable by other
            # accounts on the machine.
            checks.append(Check("token permissions", LEVEL_OK if ok else LEVEL_FAIL, reason))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("token", LEVEL_FAIL, f"could not be read: {exc!r}"))

    try:
        from .gateway.admin import admin_token_path, admin_token_permissions_ok, read_admin_token

        if read_admin_token(state_dir) is None:
            # Repaired rather than merely reported. Without this token `stop`
            # cannot reach POST /admin/v1/shutdown and every stop degrades to a
            # hard kill that severs in-flight streams -- and a gateway started
            # before the admin API existed will never mint one for itself.
            # Minting is safe under a running gateway because the admin token is
            # read per request.
            try:
                _ensure_admin_token(state_dir)
                checks.append(
                    Check(
                        "admin token",
                        LEVEL_WARN,
                        f"was absent at "
                        f"{admin_token_path(state_dir, create=False)}; minted one "
                        f"now, so `stop` can use the graceful shutdown endpoint "
                        f"instead of a hard kill",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                checks.append(
                    Check("admin token", LEVEL_FAIL, f"absent and could not be minted: {exc!r}")
                )
        else:
            checks.append(Check("admin token", LEVEL_OK, "present"))
        if read_admin_token(state_dir) is not None:
            ok, reason = admin_token_permissions_ok(state_dir)
            checks.append(
                Check("admin token permissions", LEVEL_OK if ok else LEVEL_FAIL, reason)
            )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("admin token", LEVEL_FAIL, f"could not be read: {exc!r}"))

    # --- the gateway itself ----------------------------------------------
    try:
        state = supervisor.status(resolved)
        level = LEVEL_OK if state.running else (
            LEVEL_FAIL if state.kind in (supervisor.STATUS_FOREIGN, supervisor.STATUS_CORRUPT)
            else LEVEL_WARN
        )
        checks.append(Check("gateway", level, state.detail))
    except supervisor.SupervisorError as exc:
        state = None
        checks.append(Check("gateway", LEVEL_FAIL, str(exc)))

    # --- the upstream, but only when there is a gateway to ask ------------
    if state is not None and state.running and state.port is not None:
        answer = _http_get(f"http://{supervisor.PROBE_HOST}:{state.port}/readyz")
        if answer is None:
            checks.append(
                Check("upstream", LEVEL_WARN, "the gateway stopped answering /readyz mid-check")
            )
        else:
            code, body = answer
            reason = ""
            with contextlib.suppress(Exception):
                reason = json.loads(body.decode("utf-8")).get("detail", "")
            checks.append(
                Check(
                    "upstream",
                    LEVEL_OK if code == 200 else LEVEL_WARN,
                    reason or f"/readyz answered {code}",
                )
            )
    else:
        checks.append(
            Check("upstream", LEVEL_WARN, "not checked: the gateway is not running")
        )

    return checks


def cmd_doctor(
    *,
    config: AutoRouterConfig | None = None,
    hermes_home: str | os.PathLike[str] | None = None,
    stream: Any = None,
) -> int:
    """Print every diagnostic, then exit non-zero if any of them failed."""
    out = _Printer(stream)
    checks = run_doctor(config=config, hermes_home=hermes_home)
    out.line(f"hermes-auto doctor (version {__version__})")
    out.line("")
    for check in checks:
        out.line(f"[{_MARKS[check.level]}] {check.name:<24} {check.detail}")
    failed = [check for check in checks if check.level == LEVEL_FAIL]
    warned = [check for check in checks if check.level == LEVEL_WARN]
    out.line("")
    out.line(
        f"{len(checks)} checks: {len(checks) - len(failed) - len(warned)} ok, "
        f"{len(warned)} warning(s), {len(failed)} failure(s)"
    )
    return 1 if failed else 0


class _Printer:
    """Writes to a stream, defaulting to stdout.

    Injectable so tests read what a command produced instead of capturing
    process output, and so the Hermes plugin can collect the text for a slash
    command reply rather than printing into the agent's terminal.
    """

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream

    def line(self, text: str = "") -> None:
        if self._stream is None:
            print(text)
        else:
            self._stream.write(text + "\n")
