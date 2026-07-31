"""Typed configuration for the focused deterministic auto-router.

Behavior lives in Hermes ``config.yaml``; credentials remain environment
variables named by ``credential_ref``. Absent implicit configuration falls back
to safe local defaults, while explicit missing or malformed files fail closed.
All keys owned by ``auto_router`` are validated strictly.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import urllib.parse
import warnings
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Documented defaults. Every field has one, and every one is cited.
# ---------------------------------------------------------------------------

# Public default used by the provider shim and literal ``auto`` alias.
DEFAULT_GATEWAY_URL: str = "http://127.0.0.1:8787"
# Derived from the URL above, and the source of DEFAULT_ADMIN_PORT.
DEFAULT_GATEWAY_PORT: int = 8787
DEFAULT_AUTO_START: bool = True
DEFAULT_STARTUP_TIMEOUT_SECONDS: int = 10
# Full schema validation is available but off by default for passthrough
# compatibility and latency.
DEFAULT_STRICT_VALIDATION: bool = False
# Legacy fixed-upstream defaults retained only for migration. The local default
# fails by connection-refused instead of disclosing a prompt externally.
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

# One credential-reference contract for legacy upstream and focused candidates.
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
_CANDIDATE_KEYS = frozenset(
    {
        "id",
        "provider",
        "model",
        "base_url",
        "credential_ref",
        "tier",
        "context_window",
        "supports_tools",
        "supports_vision",
    }
)
_CANDIDATE_TIERS = frozenset({"fast", "balanced", "strong"})
_ROOT_KEYS = frozenset({"gateway", "upstream", "candidates"})
_DEFAULT_CONTEXT_WINDOW = 128_000


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
    """Legacy fixed target accepted as a one-candidate migration input."""

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
class CandidateConfig:
    """One explicitly approved OpenAI-compatible routing candidate."""

    id: str
    provider: str
    model: str
    base_url: str
    credential_ref: str
    tier: str
    context_window: int
    supports_tools: bool
    supports_vision: bool

    @property
    def credential_env_var(self) -> str | None:
        if self.credential_ref == "none":
            return None
        return self.credential_ref.split(":", 1)[1]


@dataclasses.dataclass(frozen=True)
class AutoRouterConfig:
    """The focused ``auto_router:`` surface."""

    gateway: GatewayConfig = dataclasses.field(default_factory=GatewayConfig)
    # Kept as a construction-compatible migration input for callers from the
    # fixed-upstream release. Runtime routing uses ``resolved_candidates``.
    upstream: UpstreamConfig = dataclasses.field(default_factory=UpstreamConfig)
    candidates: tuple[CandidateConfig, ...] = ()
    # Where the values came from, for `doctor` output. None means "defaults".
    source_path: pathlib.Path | None = None

    @property
    def resolved_candidates(self) -> tuple[CandidateConfig, ...]:
        if self.candidates:
            return self.candidates
        upstream = self.upstream
        return (
            CandidateConfig(
                id="legacy-upstream",
                provider="openai-compatible",
                model=upstream.model,
                base_url=upstream.base_url,
                credential_ref=upstream.credential_ref,
                tier="balanced",
                context_window=_DEFAULT_CONTEXT_WINDOW,
                supports_tools=True,
                supports_vision=True,
            ),
        )


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


def _check_base_url(
    value: str, source: pathlib.Path | None, key: str
) -> str:
    """Reject a ``base_url`` carrying an embedded credential.

    ``https://user:pw@host/v1`` is a legal URL, and nothing else in this file
    stops one: ``base_url`` is validated only as a string. Refusing it here is
    the layer the operator actually sees, and it speaks in the same voice as
    :func:`_check_credential_ref` -- a config that teaches "credentials never
    belong in config.yaml" for one field while silently accepting one in the
    field next to it has not taught the rule at all.

    The output sites sanitize independently (see
    :func:`hermes_auto.telemetry.redaction.sanitize_url`); that is deliberate
    duplication, not redundancy, because every embedding caller and every test
    in this repository builds an ``UpstreamConfig`` in-process and never passes
    through this function.

    The raised message never reprints the userinfo it is rejecting.
    """
    try:
        parts = urllib.parse.urlsplit(value)
        has_userinfo = parts.username is not None or parts.password is not None
    except ValueError as exc:
        raise _fail(
            source,
            key,
            f"must be an absolute HTTP(S) URL ({type(exc).__name__})",
        ) from exc
    if has_userinfo:
        raise _fail(
            source,
            key,
            "contains an embedded credential (a 'user:password@' section). "
            "Credentials never belong in config.yaml -- remove the userinfo from "
            "the URL and put the secret in an environment variable named by "
            "'credential_ref' instead.",
        )
    try:
        host = parts.hostname
        parts.port
    except ValueError as exc:
        raise _fail(
            source,
            key,
            f"must be an absolute HTTP(S) URL ({type(exc).__name__})",
        ) from exc
    if parts.scheme.lower() not in {"http", "https"} or not host:
        raise _fail(source, key, "must be an absolute HTTP(S) URL with a host")
    return value


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
        base_url=_check_base_url(
            _as_str(
                block.get("base_url", DEFAULT_UPSTREAM_BASE_URL),
                source,
                "upstream.base_url",
            ),
            source,
            "upstream.base_url",
        ),
        model=_as_str(block.get("model", DEFAULT_UPSTREAM_MODEL), source, "upstream.model"),
        credential_ref=_check_credential_ref(
            credential_ref, source, "upstream.credential_ref"
        ),
    )


def _required_candidate_string(
    block: dict[str, Any],
    source: pathlib.Path | None,
    index: int,
    key: str,
) -> str:
    path = f"candidates[{index}].{key}"
    if key not in block:
        raise _fail(source, path, "is required")
    value = _as_str(block[key], source, path).strip()
    if not value:
        raise _fail(source, path, "must be a non-empty string")
    return value


def _build_candidates(
    raw: Any, source: pathlib.Path | None
) -> tuple[CandidateConfig, ...]:
    if not isinstance(raw, list):
        raise _fail(source, "candidates", f"expected a list, got {type(raw).__name__}")
    if not raw:
        raise _fail(source, "candidates", "must contain at least one candidate")

    candidates: list[CandidateConfig] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        block = _as_mapping(item, source, f"candidates[{index}]")
        _reject_unknown(block, _CANDIDATE_KEYS, source, f"candidates[{index}]")

        candidate_id = _required_candidate_string(block, source, index, "id")
        if candidate_id in seen_ids:
            raise _fail(
                source,
                f"candidates[{index}].id",
                f"duplicates candidate id {candidate_id!r}",
            )
        seen_ids.add(candidate_id)

        tier = _required_candidate_string(block, source, index, "tier")
        if tier not in _CANDIDATE_TIERS:
            raise _fail(
                source,
                f"candidates[{index}].tier",
                "expected one of fast, balanced, strong",
            )

        context_key = f"candidates[{index}].context_window"
        if "context_window" not in block:
            raise _fail(source, context_key, "is required")
        context_window = _as_int(block["context_window"], source, context_key)
        if context_window <= 0:
            raise _fail(source, context_key, "must be a positive integer")

        tools_key = f"candidates[{index}].supports_tools"
        vision_key = f"candidates[{index}].supports_vision"
        if "supports_tools" not in block:
            raise _fail(source, tools_key, "is required")
        if "supports_vision" not in block:
            raise _fail(source, vision_key, "is required")

        base_url_key = f"candidates[{index}].base_url"
        credential_key = f"candidates[{index}].credential_ref"
        base_url = _check_base_url(
            _required_candidate_string(block, source, index, "base_url"),
            source,
            base_url_key,
        )
        credential_ref = _check_credential_ref(
            _required_candidate_string(block, source, index, "credential_ref"),
            source,
            credential_key,
        )
        candidates.append(
            CandidateConfig(
                id=candidate_id,
                provider=_required_candidate_string(block, source, index, "provider"),
                model=_required_candidate_string(block, source, index, "model"),
                base_url=base_url,
                credential_ref=credential_ref,
                tier=tier,
                context_window=context_window,
                supports_tools=_as_bool(block["supports_tools"], source, tools_key),
                supports_vision=_as_bool(block["supports_vision"], source, vision_key),
            )
        )

    if len(candidates) == 1:
        warnings.warn(
            "Auto routing has one candidate; configure at least two candidates "
            "for a meaningful automatic selection.",
            RuntimeWarning,
            stacklevel=3,
        )
    return tuple(candidates)


def _migrate_upstream(upstream: UpstreamConfig) -> tuple[CandidateConfig, ...]:
    return (
        CandidateConfig(
            id="legacy-upstream",
            provider="openai-compatible",
            model=upstream.model,
            base_url=upstream.base_url,
            credential_ref=upstream.credential_ref,
            tier="balanced",
            context_window=_DEFAULT_CONTEXT_WINDOW,
            supports_tools=True,
            supports_vision=True,
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

    # Hermes nests product settings under `auto_router:` in its config.yaml. A
    # standalone file that omits the wrapper is also accepted, so
    # that a dedicated auto-router config does not need a redundant single key.
    if ROOT_KEY in document:
        root = _as_mapping(document[ROOT_KEY], source, ROOT_KEY)
    elif not must_exist and not set(document).issubset(_ROOT_KEYS):
        # An implicit Hermes config with no auto_router block is unrelated
        # configuration, not a malformed standalone router file.
        return AutoRouterConfig(source_path=source)
    else:
        root = document
    _reject_unknown(root, _ROOT_KEYS, source, ROOT_KEY)

    gateway_block = _as_mapping(root.get("gateway"), source, "gateway")
    upstream_block = _as_mapping(root.get("upstream"), source, "upstream")
    upstream = _build_upstream(upstream_block, source)

    if "candidates" in root and "upstream" in root:
        raise _fail(
            source,
            "auto_router",
            "configure candidates or legacy upstream, not both",
        )
    if "candidates" in root:
        candidates = _build_candidates(root["candidates"], source)
    elif "upstream" in root:
        candidates = _migrate_upstream(upstream)
        warnings.warn(
            "auto_router.upstream was migrated in memory to one candidate; "
            "run `hermes-auto configure` to approve a multi-model shortlist.",
            RuntimeWarning,
            stacklevel=2,
        )
    else:
        candidates = ()

    return AutoRouterConfig(
        gateway=_build_gateway(gateway_block, source),
        upstream=upstream,
        candidates=candidates,
        source_path=source,
    )


__all__ = [
    "ConfigError",
    "GatewayConfig",
    "UpstreamConfig",
    "CandidateConfig",
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
