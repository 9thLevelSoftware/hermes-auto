"""Unit tests for the typed auto-router configuration.

The autouse fixture clears every environment variable the resolution chain
consults. Without it these tests would read the developer's real
``$HERMES_HOME/config.yaml`` and pass or fail based on a file that is not part of
the repository.
"""

from __future__ import annotations

import dataclasses
import pathlib
import textwrap

import pytest
import yaml

from hermes_auto import config
from hermes_auto.config import (
    AutoRouterConfig,
    ConfigError,
    GatewayConfig,
    UpstreamConfig,
    load_config,
)


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.CONFIG_PATH_ENV_VAR, raising=False)
    monkeypatch.delenv(config.HERMES_HOME_ENV_VAR, raising=False)
    monkeypatch.delenv(config.EPHEMERAL_PORT_ENV_VAR, raising=False)


def write_config(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_no_configuration_anywhere_yields_full_defaults() -> None:
    """Absent is not corrupt.

    No config.yaml exists in this repository and none is created before Wave 2,
    so a loader that treated absence as fatal would break every downstream
    consumer's first verification command.
    """
    loaded = load_config()

    assert loaded == AutoRouterConfig()
    assert loaded.source_path is None
    assert loaded.gateway == GatewayConfig()
    assert loaded.upstream == UpstreamConfig()


def test_strict_validation_defaults_to_false() -> None:
    """Pinned deliberately: full request validation is off until it is measured.

    02-CONTEXT § Request Validation Policy makes this a measured decision, and
    plan 02-04 may flip it. Until then a default of True would silently spend the
    TTFT budget this phase is required to protect.
    """
    assert config.DEFAULT_STRICT_VALIDATION is False
    assert load_config().gateway.strict_validation is False


def test_documented_defaults_match_the_design_document() -> None:
    gateway = load_config().gateway

    assert gateway.url == "http://127.0.0.1:8787"
    assert gateway.port == 8787
    assert gateway.admin_port == 8788
    assert gateway.auto_start is True
    assert gateway.startup_timeout_seconds == 10
    assert gateway.state_dir is None


def test_dataclasses_are_frozen() -> None:
    loaded = load_config()

    for target, field in (
        (loaded, "gateway"),
        (loaded.gateway, "port"),
        (loaded.upstream, "model"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(target, field, "mutated")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_valid_config_round_trips(tmp_path: pathlib.Path) -> None:
    source = write_config(
        tmp_path / "config.yaml",
        """
        auto_router:
          gateway:
            url: http://127.0.0.1:9100
            auto_start: false
            startup_timeout_seconds: 45
            state_dir: /var/lib/hermes-auto
            strict_validation: true
          upstream:
            base_url: https://openrouter.ai/api/v1
            model: some-vendor/some-model
            credential_ref: env:OPENROUTER_API_KEY
        """,
    )

    loaded = load_config(source)

    assert loaded.source_path == source
    assert loaded.gateway.url == "http://127.0.0.1:9100"
    assert loaded.gateway.auto_start is False
    assert loaded.gateway.startup_timeout_seconds == 45
    assert loaded.gateway.state_dir == pathlib.Path("/var/lib/hermes-auto")
    assert loaded.gateway.strict_validation is True
    assert loaded.upstream.base_url == "https://openrouter.ai/api/v1"
    assert loaded.upstream.model == "some-vendor/some-model"
    assert loaded.upstream.credential_ref == "env:OPENROUTER_API_KEY"


def test_port_is_derived_from_the_url_so_the_two_cannot_disagree(
    tmp_path: pathlib.Path,
) -> None:
    """One port, one source of truth.

    If ``port`` defaulted to 8787 independently of ``url``, a config setting only
    the URL would bind one port while Hermes dialed another -- a failure that
    looks like "the gateway is down" and is really a config split-brain.
    """
    source = write_config(
        tmp_path / "config.yaml",
        """
        auto_router:
          gateway:
            url: http://127.0.0.1:9100
        """,
    )

    gateway = load_config(source).gateway

    assert gateway.port == 9100
    assert gateway.admin_port == 9101


def test_explicit_port_overrides_the_url_derivation(tmp_path: pathlib.Path) -> None:
    source = write_config(
        tmp_path / "config.yaml",
        """
        auto_router:
          gateway:
            url: http://127.0.0.1:9100
            port: 9500
            admin_port: 9600
        """,
    )

    gateway = load_config(source).gateway

    assert (gateway.port, gateway.admin_port) == (9500, 9600)


def test_a_bare_file_without_the_auto_router_wrapper_is_accepted(
    tmp_path: pathlib.Path,
) -> None:
    source = write_config(
        tmp_path / "standalone.yaml",
        """
        gateway:
          startup_timeout_seconds: 3
        """,
    )

    assert load_config(source).gateway.startup_timeout_seconds == 3


def test_retired_research_configuration_is_rejected(
    tmp_path: pathlib.Path,
) -> None:
    """The active product accepts only gateway, candidates, and legacy upstream."""
    source = write_config(
        tmp_path / "config.yaml",
        """
        auto_router:
          gateway:
            startup_timeout_seconds: 7
          candidates:
            - id: local-fast
              provider: local
              model: local-model
              base_url: http://127.0.0.1:11434/v1
              credential_ref: none
              tier: fast
              context_window: 128000
              supports_tools: true
              supports_vision: false
          learning:
            requirement_predictor: heuristic
        """,
    )

    with pytest.raises(ConfigError, match="learning"):
        load_config(source)


# ---------------------------------------------------------------------------
# Resolution chain
# ---------------------------------------------------------------------------


def test_hermes_home_config_is_picked_up_implicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv(config.HERMES_HOME_ENV_VAR, str(tmp_path))
    write_config(
        tmp_path / "config.yaml",
        """
        auto_router:
          gateway:
            startup_timeout_seconds: 22
        """,
    )

    loaded = load_config()

    assert loaded.gateway.startup_timeout_seconds == 22
    assert loaded.source_path == tmp_path / "config.yaml"


def test_absent_hermes_home_config_falls_through_to_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The implicit probe is allowed to come up empty; that is the whole point."""
    monkeypatch.setenv(config.HERMES_HOME_ENV_VAR, str(tmp_path))

    assert load_config() == AutoRouterConfig()


def test_config_path_env_var_takes_precedence_over_hermes_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    write_config(
        hermes_home / "config.yaml",
        "auto_router:\n  gateway:\n    startup_timeout_seconds: 1\n",
    )
    explicit = write_config(
        tmp_path / "explicit.yaml",
        "auto_router:\n  gateway:\n    startup_timeout_seconds: 99\n",
    )
    monkeypatch.setenv(config.HERMES_HOME_ENV_VAR, str(hermes_home))
    monkeypatch.setenv(config.CONFIG_PATH_ENV_VAR, str(explicit))

    assert load_config().gateway.startup_timeout_seconds == 99


# ---------------------------------------------------------------------------
# Typed failures. Absent is not corrupt, but a named-and-missing file is wrong.
# ---------------------------------------------------------------------------


def test_explicitly_named_missing_file_raises(tmp_path: pathlib.Path) -> None:
    """Naming a path asserts it exists.

    Handing back defaults for a mistyped explicit path would turn a typo into a
    gateway silently running against the wrong upstream.
    """
    missing = tmp_path / "nope.yaml"

    with pytest.raises(ConfigError) as caught:
        load_config(missing)

    assert str(missing) in str(caught.value)


def test_missing_file_named_by_env_var_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    missing = tmp_path / "nope.yaml"
    monkeypatch.setenv(config.CONFIG_PATH_ENV_VAR, str(missing))

    with pytest.raises(ConfigError) as caught:
        load_config()

    assert config.CONFIG_PATH_ENV_VAR in str(caught.value)


def test_malformed_yaml_raises_config_error_not_a_yaml_error(
    tmp_path: pathlib.Path,
) -> None:
    """No ``yaml.YAMLError`` may escape a public function."""
    source = write_config(tmp_path / "config.yaml", "auto_router:\n  gateway: [unclosed\n")

    with pytest.raises(ConfigError) as caught:
        load_config(source)

    assert str(source) in str(caught.value)
    # The underlying yaml error is preserved as the cause but must not be what
    # the caller has to catch.
    assert not isinstance(caught.value, yaml.YAMLError)
    assert isinstance(caught.value.__cause__, yaml.YAMLError)


def test_non_mapping_top_level_raises(tmp_path: pathlib.Path) -> None:
    source = write_config(tmp_path / "config.yaml", "- just\n- a\n- list\n")

    with pytest.raises(ConfigError, match="mapping"):
        load_config(source)


@pytest.mark.parametrize(
    "literal_secret",
    [
        "sk-proj-abc123def456ghi789",
        "ghp_1234567890abcdefghij",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def",
        "hunter2",
        "ENV:OPENROUTER_API_KEY",
        "env:lowercase_name",
        "",
    ],
)
def test_literal_credential_is_rejected_with_an_actionable_message(
    tmp_path: pathlib.Path, literal_secret: str
) -> None:
    """The constraint "credentials only in env vars" made enforceable.

    The message must say where credentials belong, because the person who hits
    this is mid-edit in a config file and needs the fix, not the rule.
    """
    source = write_config(
        tmp_path / "config.yaml",
        f"auto_router:\n  upstream:\n    credential_ref: {literal_secret!r}\n",
    )

    with pytest.raises(ConfigError) as caught:
        load_config(source)

    message = str(caught.value)
    assert "upstream.credential_ref" in message
    assert "environment variable" in message
    assert config.CREDENTIAL_REF_PATTERN in message


def test_credential_ref_pattern_is_the_focused_contract() -> None:
    """The exported pattern accepts only none or an environment reference."""
    import re

    pattern = re.compile(config.CREDENTIAL_REF_PATTERN)
    assert pattern.fullmatch("none")
    assert pattern.fullmatch("env:PROVIDER_KEY")
    assert not pattern.fullmatch("literal-secret")


@pytest.mark.parametrize(
    "accepted", ["none", "env:OPENROUTER_API_KEY", "env:A", "env:X_1"]
)
def test_accepted_credential_references(
    tmp_path: pathlib.Path, accepted: str
) -> None:
    source = write_config(
        tmp_path / "config.yaml",
        f"auto_router:\n  upstream:\n    credential_ref: {accepted}\n",
    )

    assert load_config(source).upstream.credential_ref == accepted


def test_credential_env_var_exposes_the_name_never_a_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-should-never-be-read-here")
    source = write_config(
        tmp_path / "config.yaml",
        "auto_router:\n  upstream:\n    credential_ref: env:OPENROUTER_API_KEY\n",
    )

    upstream = load_config(source).upstream

    assert upstream.credential_env_var == "OPENROUTER_API_KEY"
    assert "sk-should-never-be-read-here" not in repr(upstream)


def test_unknown_key_inside_an_owned_block_is_rejected(tmp_path: pathlib.Path) -> None:
    """A typo in a security flag must never silently become its default.

    ``strict_validaton`` is the realistic case: accepted-and-ignored, it reads as
    "validation configured" to whoever wrote it while nothing validates.
    """
    source = write_config(
        tmp_path / "config.yaml",
        "auto_router:\n  gateway:\n    strict_validaton: true\n",
    )

    with pytest.raises(ConfigError) as caught:
        load_config(source)

    assert "gateway.strict_validaton" in str(caught.value)
    assert "unknown key" in str(caught.value)


@pytest.mark.parametrize(
    ("body", "expected_key"),
    [
        ("gateway:\n  auto_start: yes-please\n", "gateway.auto_start"),
        ("gateway:\n  startup_timeout_seconds: soon\n", "gateway.startup_timeout_seconds"),
        ("gateway:\n  strict_validation: 1\n", "gateway.strict_validation"),
        ("gateway:\n  url: 8787\n", "gateway.url"),
        ("gateway:\n  port: eighty\n", "gateway.port"),
        ("upstream:\n  model: 42\n", "upstream.model"),
        ("gateway: not-a-mapping\n", "gateway"),
    ],
)
def test_wrong_types_raise_config_error_naming_the_key(
    tmp_path: pathlib.Path, body: str, expected_key: str
) -> None:
    """No bare ``TypeError`` or ``KeyError`` escapes ``load_config``."""
    source = write_config(tmp_path / "config.yaml", body)

    with pytest.raises(ConfigError) as caught:
        load_config(source)

    assert expected_key in str(caught.value)


def test_boolean_one_is_not_accepted_as_true(tmp_path: pathlib.Path) -> None:
    """``isinstance(True, int)`` is True in Python; the converse must not hold.

    Accepting ``1`` for ``strict_validation`` would also accept ``0`` and, more
    dangerously, a quoted ``"false"`` elsewhere -- which is truthy.
    """
    source = write_config(
        tmp_path / "config.yaml", "gateway:\n  strict_validation: 1\n"
    )

    with pytest.raises(ConfigError, match="expected a boolean"):
        load_config(source)


@pytest.mark.parametrize("port", [-1, 70000, 65536])
def test_out_of_range_port_is_rejected(tmp_path: pathlib.Path, port: int) -> None:
    source = write_config(tmp_path / "config.yaml", f"gateway:\n  port: {port}\n")

    with pytest.raises(ConfigError, match="out of range"):
        load_config(source)


def test_ephemeral_port_requires_explicit_test_opt_in(tmp_path: pathlib.Path) -> None:
    """Hermes's ``model.base_url`` is static config and cannot follow port 0."""
    source = write_config(tmp_path / "config.yaml", "gateway:\n  port: 0\n")

    with pytest.raises(ConfigError, match=config.EPHEMERAL_PORT_ENV_VAR):
        load_config(source)


def test_ephemeral_port_is_allowed_under_the_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv(config.EPHEMERAL_PORT_ENV_VAR, "1")
    source = write_config(tmp_path / "config.yaml", "gateway:\n  port: 0\n")

    gateway = load_config(source).gateway

    assert (gateway.port, gateway.admin_port) == (0, 0)


def test_admin_port_equal_to_port_is_rejected(tmp_path: pathlib.Path) -> None:
    """The admin API is a separate authentication scope (design.md 5.3).

    Collapsing both onto one port would put the runtime-reroute surface behind
    the inference token, which is the privilege-separation gap the threat model
    already flags as uncovered by design.md 21.
    """
    source = write_config(
        tmp_path / "config.yaml", "gateway:\n  port: 9000\n  admin_port: 9000\n"
    )

    with pytest.raises(ConfigError, match="must differ"):
        load_config(source)


def test_non_positive_startup_timeout_is_rejected(tmp_path: pathlib.Path) -> None:
    source = write_config(
        tmp_path / "config.yaml", "gateway:\n  startup_timeout_seconds: 0\n"
    )

    with pytest.raises(ConfigError, match="positive"):
        load_config(source)


def test_empty_file_is_treated_as_fully_defaulted(tmp_path: pathlib.Path) -> None:
    source = write_config(tmp_path / "config.yaml", "")

    loaded = load_config(source)

    assert loaded.gateway == GatewayConfig()
    assert loaded.source_path == source


def test_loader_uses_safe_load_only() -> None:
    """``yaml.load`` with the default Loader turns config editing into code execution."""
    source = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    calls = [
        line
        for line in source.splitlines()
        if "yaml.load(" in line and not line.strip().startswith("#")
    ]

    assert calls == []
    assert "yaml.safe_load(" in source


def test_arbitrary_python_tags_do_not_construct_objects(tmp_path: pathlib.Path) -> None:
    """The behavioral proof that safe_load is in force, not just a grep of it."""
    source = write_config(
        tmp_path / "config.yaml",
        "gateway: !!python/object/apply:os.system ['echo pwned']\n",
    )

    with pytest.raises(ConfigError):
        load_config(source)
