"""Typed configuration for the auto-router sidecar.

This module makes one project constraint *enforceable* rather than remembered:
**behavioral settings live in config.yaml; only credentials and generated tokens
live in environment variables** (02-CONTEXT § Phase-Wide Constraints item 5).
``credential_ref`` therefore accepts an environment-variable *reference* and
nothing else. A literal secret is rejected at load time with a message saying
where it belongs, so the failure happens on the developer's machine rather than
in a config file that gets committed.

Three contracts are frozen here and are read by plans 02-04 through 02-07.

**Absent is not corrupt.** ``load_config(None)`` walks a resolution chain and
falls through to fully-defaulted values when no config file exists. That is not
leniency, it is the same distinction the runtime file draws: a missing file means
"nothing configured", a present-but-unparseable file means "something is wrong".
No ``config.yaml`` exists in this repository, and none is created before Wave 2,
so a loader that treated absence as fatal would break every consumer's first
verification command (02-CONTEXT § Configuration Contract).

**An explicitly named path is an assertion that it exists.** ``load_config(p)``
with an explicit ``p``, and a ``HERMES_AUTO_CONFIG`` that names a missing file,
both raise ``ConfigError``. The caller asserted the file is there; silently
handing back defaults would turn a typo in a path into a gateway that quietly
runs against the wrong upstream. Only the *implicit* probe of
``$HERMES_HOME/config.yaml`` is allowed to come up empty.

**Unknown keys are rejected inside the blocks this phase owns, and ignored
outside them.** A typo like ``strict_validaton`` under ``gateway:`` must be an
error -- silently defaulting it to False is precisely the failure that makes a
security setting untrustworthy. But design.md 16's ``auto_router:`` block also
carries ``routing:``, ``constraints:``, ``candidates:``, ``telemetry:`` and
``learning:``, which later phases own and this phase cannot validate. Rejecting
those would make a correct, complete config.yaml unloadable in Phase 2. So the
rule is: strict where we own the schema, permissive where we do not yet.

Errors are typed. No bare ``KeyError``, ``TypeError``, ``ValueError``, or
``yaml.YAMLError`` escapes ``load_config``.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import urllib.parse
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Documented defaults. Every field has one, and every one is cited.
# ---------------------------------------------------------------------------

# design.md 16: auto_router.gateway.url: http://127.0.0.1:8787
DEFAULT_GATEWAY_URL: str = "http://127.0.0.1:8787"
# Derived from the URL above, and the source of DEFAULT_ADMIN_PORT.
DEFAULT_GATEWAY_PORT: int = 8787
# design.md 16
DEFAULT_AUTO_START: bool = True
DEFAULT_STARTUP_TIMEOUT_SECONDS: int = 10
# 02-CONTEXT § Request Validation Policy: full request-schema validation is
# hoisted at startup and runs unconditionally in the CI contract suite, but is
# OFF at runtime until plan 02-04 measures the hoisted cost. Measurement decides
# whether this flips, not taste.
DEFAULT_STRICT_VALIDATION: bool = False
# Phase 2 forwards every request to one fixed OpenAI-compatible endpoint. The
# default points at a local Ollama-style server so a zero-config run fails by
# connection-refused rather than by leaking a prompt to a third party.
DEFAULT_UPSTREAM_BASE_URL: str = "http://127.0.0.1:11434/v1"
DEFAULT_UPSTREAM_MODEL: str = ""
# design.md 16 uses `none` for an unauthenticated local endpoint.
DEFAULT_CREDENTIAL_REF: str = "none"

# Environment variable naming an explicit config file. Set => the file must exist.
CONFIG_PATH_ENV_VAR: str = "HERMES_AUTO_CONFIG"
# Hermes's own home directory; its config.yaml carries the `auto_router:` block.
HERMES_HOME_ENV_VAR: str = "HERMES_HOME"
CONFIG_FILE_NAME: str = "config.yaml"
# The only top-level block in a Hermes config.yaml that belongs to this project.
ROOT_KEY: str = "auto_router"

# Frozen in Phase 1 by data/schema/routing/model-card.v1.schema.json. Reused here
# verbatim so a candidate's credential_ref and the gateway's upstream
# credential_ref cannot drift into two different notions of "safe".
CREDENTIAL_REF_PATTERN: str = r"^(env:[A-Z0-9_]+|none)$"
_CREDENTIAL_REF_RE = re.compile(CREDENTIAL_REF_PATTERN)

# 02-CONTEXT § Configuration Contract: port 0 asks the OS for an ephemeral port,
# which Hermes cannot follow because `model.base_url` is static config. Permitted
# only when a test explicitly opts in.
EPHEMERAL_PORT_ENV_VAR: str = "HERMES_AUTO_TEST_EPHEMERAL"

_GATEWAY_KEYS = frozenset(
    {
        "url",
        "port",
        "admin_port",
        "auto_start",
        "startup_timeout_seconds",
        "state_dir",
        "strict_validation",
    }
)
_UPSTREAM_KEYS = frozenset({"base_url", "model", "credential_ref"})


class ConfigError(Exception):
    """Configuration is present but unusable.

    Raised for an unparseable file, a wrong-typed or unknown key inside a block
    this phase owns, an out-of-range port, an explicitly named file that does not
    exist, and a ``credential_ref`` that looks like a literal secret. Never raised
    merely because no configuration exists.
    """


@dataclasses.dataclass(frozen=True)
class GatewayConfig:
    """The local gateway's binding and startup behavior (design.md 16)."""

    url: str = DEFAULT_GATEWAY_URL
    port: int = DEFAULT_GATEWAY_PORT
    admin_port: int = DEFAULT_GATEWAY_PORT + 1
    auto_start: bool = DEFAULT_AUTO_START
    startup_timeout_seconds: int = DEFAULT_STARTUP_TIMEOUT_SECONDS
    # None means "not configured"; resolve through
    # hermes_auto.state.paths.state_dir(configured=...) so the
    # HERMES_AUTO_STATE_DIR override keeps its precedence. Deliberately not
    # resolved at load time: loading configuration must not create directories.
    state_dir: pathlib.Path | None = None
    strict_validation: bool = DEFAULT_STRICT_VALIDATION


@dataclasses.dataclass(frozen=True)
class UpstreamConfig:
    """The single fixed passthrough target for Phase 2.

    Phase 2 proves a negative claim -- that one fixed candidate behaves
    identically through the gateway and called directly -- so there is exactly
    one upstream and no selection logic. Phase 7 replaces this with real
    candidates; that is why there is no adapter seam here yet.
    """

    base_url: str = DEFAULT_UPSTREAM_BASE_URL
    model: str = DEFAULT_UPSTREAM_MODEL
    credential_ref: str = DEFAULT_CREDENTIAL_REF

    @property
    def credential_env_var(self) -> str | None:
        """The environment variable holding the credential, or None for ``none``.

        Returns the variable *name*, never its value. Resolution stays with the
        caller so this object can be logged or dumped without a credential ever
        having been read into it.
        """
        if self.credential_ref == "none":
            return None
        return self.credential_ref.split(":", 1)[1]


@dataclasses.dataclass(frozen=True)
class AutoRouterConfig:
    """The whole ``auto_router:`` surface this phase understands."""

    gateway: GatewayConfig = dataclasses.field(default_factory=GatewayConfig)
    upstream: UpstreamConfig = dataclasses.field(default_factory=UpstreamConfig)
    # Where the values came from, for `doctor` output. None means "defaults".
    source_path: pathlib.Path | None = None


# ---------------------------------------------------------------------------
# Coercion helpers. Each converts a would-be bare TypeError into a ConfigError
# that names the file and the offending key.
# ---------------------------------------------------------------------------


def _fail(source: pathlib.Path | None, key: str, detail: str) -> ConfigError:
    where = str(source) if source is not None else "<built-in defaults>"
    return ConfigError(f"{where}: {key}: {detail}")


def _as_mapping(
    value: Any, source: pathlib.Path | None, key: str
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _fail(source, key, f"expected a mapping, got {type(value).__name__}")
    return value


def _as_bool(value: Any, source: pathlib.Path | None, key: str) -> bool:
    # Strict: YAML already turns `true`/`yes`/`on` into a bool. A string here
    # means the value was quoted, and "false" is truthy, which is the kind of
    # silent inversion that makes a security flag worthless.
    if not isinstance(value, bool):
        raise _fail(
            source, key, f"expected a boolean, got {type(value).__name__} ({value!r})"
        )
    return value


def _as_int(value: Any, source: pathlib.Path | None, key: str) -> int:
    # `isinstance(True, int)` is True in Python, so bools are excluded explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(
            source, key, f"expected an integer, got {type(value).__name__} ({value!r})"
        )
    return value


def _as_str(value: Any, source: pathlib.Path | None, key: str) -> str:
    if not isinstance(value, str):
        raise _fail(
            source, key, f"expected a string, got {type(value).__name__} ({value!r})"
        )
    return value


def _reject_unknown(
    block: dict[str, Any],
    allowed: frozenset[str],
    source: pathlib.Path | None,
    prefix: str,
) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise _fail(
            source,
            f"{prefix}.{unknown[0]}",
            "unknown key; expected one of " + ", ".join(sorted(allowed)),
        )


def _check_port(value: int, source: pathlib.Path | None, key: str) -> int:
    if value == 0:
        if os.environ.get(EPHEMERAL_PORT_ENV_VAR) == "1":
            return value
        raise _fail(
            source,
            key,
            "port 0 requests an ephemeral port, which Hermes's static "
            f"model.base_url cannot follow; set {EPHEMERAL_PORT_ENV_VAR}=1 to "
            "allow it in tests",
        )
    if not 1 <= value <= 65535:
        raise _fail(source, key, f"port out of range 1-65535: {value}")
    return value


def _port_from_url(url: str, source: pathlib.Path | None) -> int:
    """Derive the listening port from ``gateway.url``.

    ``port`` is derived rather than independently defaulted so that a config
    setting only ``url: http://127.0.0.1:9000`` cannot end up binding 8787 while
    Hermes talks to 9000. Two independent sources of truth for one port is a
    defect waiting for a support ticket.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:  # non-numeric or out-of-range port in the URL
        raise _fail(source, "gateway.url", f"unparseable URL {url!r}: {exc}") from exc
    if port is not None:
        return port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return DEFAULT_GATEWAY_PORT


def _check_credential_ref(
    value: str, source: pathlib.Path | None, key: str
) -> str:
    if _CREDENTIAL_REF_RE.match(value):
        return value
    raise _fail(
        source,
        key,
        f"{value!r} is not a credential reference. Credentials never belong in "
        "config.yaml -- put the secret in an environment variable and write "
        "'env:VAR_NAME' here (or 'none' for an unauthenticated local endpoint). "
        f"Accepted pattern: {CREDENTIAL_REF_PATTERN}",
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _resolve_path(path: str | os.PathLike[str] | None) -> tuple[pathlib.Path | None, bool]:
    """Return ``(path_or_None, must_exist)`` for the resolution chain.

    ``must_exist`` is True when a human named the file explicitly, and False for
    the implicit ``$HERMES_HOME/config.yaml`` probe. That flag is the whole
    difference between "you have no config" and "your config path is wrong".
    """
    if path is not None:
        return pathlib.Path(os.fspath(path)).expanduser(), True

    from_env = os.environ.get(CONFIG_PATH_ENV_VAR)
    if from_env and from_env.strip():
        return pathlib.Path(from_env).expanduser(), True

    hermes_home = os.environ.get(HERMES_HOME_ENV_VAR)
    if hermes_home and hermes_home.strip():
        return pathlib.Path(hermes_home).expanduser() / CONFIG_FILE_NAME, False

    return None, False


def _read_yaml(source: pathlib.Path) -> dict[str, Any]:
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{source}: cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source}: is not valid UTF-8: {exc}") from exc

    try:
        # safe_load, never load: yaml.load with the default Loader constructs
        # arbitrary Python objects from a config file, which turns "edit
        # config.yaml" into "execute code".
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: is not valid YAML: {exc}") from exc

    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ConfigError(
            f"{source}: top level must be a mapping, got {type(document).__name__}"
        )
    return document


def _build_gateway(
    block: dict[str, Any], source: pathlib.Path | None
) -> GatewayConfig:
    _reject_unknown(block, _GATEWAY_KEYS, source, "gateway")

    url = _as_str(block.get("url", DEFAULT_GATEWAY_URL), source, "gateway.url")

    if "port" in block:
        port = _check_port(_as_int(block["port"], source, "gateway.port"), source, "gateway.port")
    else:
        port = _check_port(_port_from_url(url, source), source, "gateway.url")

    if "admin_port" in block:
        admin_port = _check_port(
            _as_int(block["admin_port"], source, "gateway.admin_port"),
            source,
            "gateway.admin_port",
        )
    else:
        # port + 1 by default; an ephemeral inference port yields an ephemeral
        # admin port rather than the nonsensical port 1.
        admin_port = 0 if port == 0 else _check_port(port + 1, source, "gateway.admin_port")

    if admin_port != 0 and admin_port == port:
        raise _fail(
            source,
            "gateway.admin_port",
            "must differ from gateway.port: the admin API is a separate "
            "authentication scope on its own listener (design.md 5.3)",
        )

    raw_state_dir = block.get("state_dir")
    state_dir: pathlib.Path | None = None
    if raw_state_dir is not None:
        text = _as_str(raw_state_dir, source, "gateway.state_dir")
        if text.strip():
            state_dir = pathlib.Path(os.path.expandvars(text)).expanduser()

    timeout = _as_int(
        block.get("startup_timeout_seconds", DEFAULT_STARTUP_TIMEOUT_SECONDS),
        source,
        "gateway.startup_timeout_seconds",
    )
    if timeout <= 0:
        raise _fail(
            source,
            "gateway.startup_timeout_seconds",
            f"must be a positive number of seconds, got {timeout}",
        )

    return GatewayConfig(
        url=url,
        port=port,
        admin_port=admin_port,
        auto_start=_as_bool(
            block.get("auto_start", DEFAULT_AUTO_START), source, "gateway.auto_start"
        ),
        startup_timeout_seconds=timeout,
        state_dir=state_dir,
        strict_validation=_as_bool(
            block.get("strict_validation", DEFAULT_STRICT_VALIDATION),
            source,
            "gateway.strict_validation",
        ),
    )


def _build_upstream(
    block: dict[str, Any], source: pathlib.Path | None
) -> UpstreamConfig:
    _reject_unknown(block, _UPSTREAM_KEYS, source, "upstream")

    credential_ref = _as_str(
        block.get("credential_ref", DEFAULT_CREDENTIAL_REF),
        source,
        "upstream.credential_ref",
    )
    return UpstreamConfig(
        base_url=_as_str(
            block.get("base_url", DEFAULT_UPSTREAM_BASE_URL), source, "upstream.base_url"
        ),
        model=_as_str(block.get("model", DEFAULT_UPSTREAM_MODEL), source, "upstream.model"),
        credential_ref=_check_credential_ref(
            credential_ref, source, "upstream.credential_ref"
        ),
    )


def load_config(path: str | os.PathLike[str] | None = None) -> AutoRouterConfig:
    """Load the ``auto_router:`` configuration.

    Resolution order when ``path`` is None: ``HERMES_AUTO_CONFIG`` ->
    ``$HERMES_HOME/config.yaml`` -> built-in defaults.

    Raises ``ConfigError`` -- and only ``ConfigError`` -- for a file that is
    present but unusable, or for an explicitly named file that is absent. Returns
    a fully-defaulted config when nothing is configured at all.
    """
    source, must_exist = _resolve_path(path)

    if source is None:
        return AutoRouterConfig()

    if not source.is_file():
        if must_exist:
            raise ConfigError(
                f"{source}: configuration file does not exist. It was named "
                "explicitly (argument or "
                f"{CONFIG_PATH_ENV_VAR}), so it is required; omit it to fall back "
                "to built-in defaults."
            )
        return AutoRouterConfig()

    document = _read_yaml(source)

    # design.md 16 nests everything under `auto_router:` inside Hermes's own
    # config.yaml. A standalone file that omits the wrapper is also accepted, so
    # that a dedicated auto-router config does not need a redundant single key.
    if ROOT_KEY in document:
        root = _as_mapping(document[ROOT_KEY], source, ROOT_KEY)
    else:
        root = document

    # Sibling blocks (routing, constraints, candidates, telemetry, learning) are
    # deliberately not validated here; see the module docstring.
    gateway_block = _as_mapping(root.get("gateway"), source, "gateway")
    upstream_block = _as_mapping(root.get("upstream"), source, "upstream")

    return AutoRouterConfig(
        gateway=_build_gateway(gateway_block, source),
        upstream=_build_upstream(upstream_block, source),
        source_path=source,
    )


__all__ = [
    "ConfigError",
    "GatewayConfig",
    "UpstreamConfig",
    "AutoRouterConfig",
    "load_config",
    "CREDENTIAL_REF_PATTERN",
    "CONFIG_PATH_ENV_VAR",
    "HERMES_HOME_ENV_VAR",
    "ROOT_KEY",
    "DEFAULT_GATEWAY_URL",
    "DEFAULT_GATEWAY_PORT",
    "DEFAULT_AUTO_START",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "DEFAULT_STRICT_VALIDATION",
    "DEFAULT_UPSTREAM_BASE_URL",
    "DEFAULT_UPSTREAM_MODEL",
    "DEFAULT_CREDENTIAL_REF",
]
