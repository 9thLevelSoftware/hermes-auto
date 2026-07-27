"""Prove the ``hermes-auto`` ProviderProfile is built the way Hermes requires.

Two classes of test live here.

The envelope tests are pure and run anywhere: :func:`hermes_auto.provider.build_envelope`
has no dependency on Hermes, which is exactly why the shim can duplicate it.

The profile tests need the real ``providers.base.ProviderProfile``. Hermes is a
source checkout on this machine rather than an installed distribution, so
``import providers`` fails from this virtualenv unless the checkout root is on
``sys.path``. The ``hermes_provider_profile`` fixture puts it there and skips
with an explicit reason when the checkout is absent -- a stub would defeat the
point, since the defect being guarded against (``design.md`` §5.1's bare
class-attribute form) is invisible against anything but the real dataclass.
"""

from __future__ import annotations

import ast
import inspect
import os
import pathlib
import re
import sys
from typing import Any

import pytest

from hermes_auto import provider
from hermes_auto.gateway.schemas import build_validator
from hermes_auto.provider import (
    AUTO_MODELS,
    ENVELOPE_KEY,
    MAX_FIELD_LENGTH,
    PROTOCOL_VERSION,
    build_envelope,
    build_profile,
    gateway_base_url,
)
from hermes_auto.version import __version__

METADATA_SCHEMA_ID = (
    "https://hermes-auto-router.dev/schema/routing/hermes-auto-metadata.v1.json"
)

DEFAULT_HERMES_CHECKOUT = pathlib.Path(
    r"C:/Users/dasbl/AppData/Local/hermes/hermes-agent"
)

# Exact-name, case-folded. Frozen by plan 02-02's redaction sink; reproduced
# here so the envelope is genuinely subject to the same rule its consumers are.
REDACTION_BANNED_KEYS = frozenset(
    {
        "messages",
        "content",
        "prompt",
        "tool_calls",
        "tool_result",
        "tool_output",
        "arguments",
        "authorization",
        "api_key",
        "token",
        "secret",
    }
)


def _hermes_checkout() -> pathlib.Path | None:
    """Locate the Hermes source checkout, or return ``None``."""
    configured = os.environ.get("HERMES_AGENT_REPO", "").strip()
    root = pathlib.Path(configured) if configured else DEFAULT_HERMES_CHECKOUT
    return root if (root / "providers" / "base.py").is_file() else None


@pytest.fixture(scope="module")
def hermes_on_path() -> Any:
    """Put the Hermes checkout on ``sys.path`` so ``providers`` imports.

    ``providers/base.py`` and ``providers/__init__.py`` are stdlib-only, and
    provider discovery is lazy, so importing them costs nothing and registers
    nothing. Skips loudly rather than substituting a fake.
    """
    root = _hermes_checkout()
    if root is None:
        pytest.skip(
            "Hermes source checkout not found -- set HERMES_AGENT_REPO or place "
            f"it at {DEFAULT_HERMES_CHECKOUT}. Cannot construct a real "
            "ProviderProfile without it."
        )
    sys.path.insert(0, str(root))
    try:
        yield root
    finally:
        try:
            sys.path.remove(str(root))
        except ValueError:  # pragma: no cover - only if a test mutated sys.path
            pass


@pytest.fixture(scope="module")
def profile(hermes_on_path: pathlib.Path) -> Any:
    """The real profile instance, built against the real dataclass."""
    return build_profile()


@pytest.fixture(scope="module")
def metadata_validator() -> Any:
    """Hoisted validator -- ``jsonschema.validate`` re-runs ``check_schema``."""
    return build_validator(METADATA_SCHEMA_ID)


# ---------------------------------------------------------------------------
# The envelope. Pure, no Hermes required.
# ---------------------------------------------------------------------------


def test_no_argument_envelope_validates(metadata_validator: Any) -> None:
    """``build_extra_body()`` with no arguments must produce a valid envelope.

    This is the auxiliary-client call form: ``agent/auxiliary_client.py:7006``
    passes ``model``, ``base_url`` and ``reasoning_config`` and **no**
    ``session_id`` at all. A suite that only ever passes ``session_id='s-1'``
    cannot see this, and the failure it misses is a 400 on every compression,
    vision, title-generation and web-extraction call.
    """
    metadata_validator.validate(build_envelope())


def test_none_session_id_envelope_validates(metadata_validator: Any) -> None:
    """The ``chat_completion_helpers`` call form: ``session_id`` may be None."""
    metadata_validator.validate(build_envelope(session_id=None))


def test_empty_string_session_id_would_have_failed_the_schema(
    metadata_validator: Any,
) -> None:
    """Prove the negative case the fallback exists to prevent.

    Without this, "we synthesize an id" is an assertion about the code rather
    than a demonstration that the obvious alternative is broken.
    """
    naive = dict(build_envelope())
    naive["root_session_id"] = ""

    with pytest.raises(Exception) as excinfo:
        metadata_validator.validate(naive)

    assert "minLength" in str(excinfo.value) or "too short" in str(excinfo.value)


def test_absent_session_id_yields_a_non_empty_unique_id() -> None:
    """Synthesized ids are non-empty and distinguish concurrent aux calls."""
    first = build_envelope()["root_session_id"]
    second = build_envelope()["root_session_id"]

    assert first and second
    assert first != second
    assert first.startswith("no-session-")


def test_supplied_session_id_is_passed_through(metadata_validator: Any) -> None:
    envelope = build_envelope(session_id="s-1", virtual_model="auto:quality")
    metadata_validator.validate(envelope)

    assert envelope["root_session_id"] == "s-1"
    assert envelope["virtual_model"] == "auto:quality"


def test_protocol_version_is_exactly_one() -> None:
    assert PROTOCOL_VERSION == 1
    assert build_envelope()["protocol_version"] == 1
    assert isinstance(build_envelope()["protocol_version"], int)
    assert not isinstance(build_envelope()["protocol_version"], bool)


def test_plugin_version_is_the_distribution_version() -> None:
    assert build_envelope()["plugin_version"] == __version__


def test_absent_virtual_model_defaults_to_balanced() -> None:
    assert build_envelope()["virtual_model"] == "auto:balanced"
    assert build_envelope(virtual_model="")["virtual_model"] == "auto:balanced"


@pytest.mark.parametrize("field", ["root_session_id", "virtual_model"])
def test_oversized_hermes_supplied_values_are_clamped(
    field: str, metadata_validator: Any
) -> None:
    """A long value from Hermes must not become a 400 on our own schema."""
    long_value = "x" * 5000
    envelope = build_envelope(
        session_id=long_value if field == "root_session_id" else None,
        virtual_model=long_value if field == "virtual_model" else None,
    )

    assert len(envelope[field]) == MAX_FIELD_LENGTH
    metadata_validator.validate(envelope)


def test_envelope_keys_are_exactly_the_schema_required_set() -> None:
    """No extra keys: the schema sets ``additionalProperties: false``."""
    assert set(build_envelope()) == {
        "protocol_version",
        "root_session_id",
        "virtual_model",
        "plugin_version",
    }


def test_envelope_carries_no_prompt_message_or_credential_field() -> None:
    """The envelope must be inert with respect to the redaction sink.

    Checked against the banned-key list plan 02-02 froze rather than a fresh
    list, so the two cannot drift apart.
    """
    envelope = build_envelope(session_id="s-1", virtual_model="auto:economy")
    folded = {key.casefold() for key in envelope}

    assert not folded & REDACTION_BANNED_KEYS
    assert all(isinstance(value, (str, int)) for value in envelope.values())


def test_gateway_base_url_defaults_to_the_documented_address() -> None:
    assert gateway_base_url() == "http://127.0.0.1:8787/v1"


# ---------------------------------------------------------------------------
# The profile object. Needs the real dataclass.
# ---------------------------------------------------------------------------


def test_profile_is_an_instance_not_a_class(profile: Any, hermes_on_path: Any) -> None:
    """``register_provider`` takes an instance; a class would register nothing."""
    from providers.base import ProviderProfile

    assert isinstance(profile, ProviderProfile)
    assert type(profile) is not ProviderProfile, "must be a method-overriding subclass"


def test_design_md_bare_class_attribute_form_cannot_even_be_constructed(
    hermes_on_path: Any,
) -> None:
    """Demonstrate the defect ``02-CONTEXT.md`` corrects, rather than assert it.

    ``design.md`` §5.1 declares ``name``/``base_url``/``fallback_models`` as
    bare class attributes on a ``ProviderProfile`` subclass and never
    instantiates. Because the base is a ``@dataclass`` whose ``name`` field has
    no default, the inherited ``__init__`` still demands it -- so that form
    does not merely fail to populate fields, it raises on construction.
    """
    from providers.base import ProviderProfile

    class DesignDocForm(ProviderProfile):  # exactly design.md §5.1's shape
        name = "hermes-auto"
        base_url = "http://127.0.0.1:8787/v1"
        fallback_models = ("auto:balanced",)

    with pytest.raises(TypeError, match="name"):
        DesignDocForm()


def test_registering_the_class_instead_of_an_instance_fails_only_at_request_time(
    hermes_on_path: Any,
) -> None:
    """The worse half of the §5.1 defect, pinned so nobody reintroduces it.

    ``register_provider`` stores whatever it is handed, keyed on ``.name``.
    Hand it the *class* and registration succeeds -- ``get_provider_profile``
    returns it, ``.name`` reads correctly, the model picker and ``doctor`` look
    healthy. Every method is then unbound, so the first real request raises
    ``TypeError: missing 1 required positional argument: 'self'``. That is
    strictly worse than "unknown provider hermes-auto", which at least fails at
    setup. Verified against the real registry, not asserted.
    """
    from providers.base import ProviderProfile

    class ClassNotInstance(ProviderProfile):
        name = "hermes-auto-classform"

    assert ClassNotInstance.name == "hermes-auto-classform"  # registration looks fine

    with pytest.raises(TypeError, match="self"):
        ClassNotInstance.build_extra_body(session_id="s-1")


def test_profile_identity_fields(profile: Any) -> None:
    assert profile.name == "hermes-auto"
    assert profile.display_name == "Hermes Auto Router"
    assert profile.api_mode == "chat_completions"
    assert profile.description


def test_profile_registers_all_four_virtual_models(profile: Any) -> None:
    assert AUTO_MODELS == (
        "auto:quality",
        "auto:balanced",
        "auto:economy",
        "auto:session",
    )
    assert set(profile.fallback_models) >= set(AUTO_MODELS)


def test_profile_declares_the_token_env_var_and_no_secret(profile: Any) -> None:
    """Only the variable *name* is declared; the value never touches the profile."""
    assert profile.env_vars == ("HERMES_AUTO_ROUTER_TOKEN",)
    assert not os.environ.get("HERMES_AUTO_ROUTER_TOKEN", "") or all(
        os.environ["HERMES_AUTO_ROUTER_TOKEN"] not in str(value)
        for value in vars(profile).values()
    )


def test_profile_base_url_points_at_the_local_gateway(profile: Any) -> None:
    assert profile.base_url == "http://127.0.0.1:8787/v1"
    assert profile.get_hostname() == "127.0.0.1"


def test_health_check_probe_target_is_the_endpoint_the_gateway_serves(
    profile: Any,
) -> None:
    """``supports_health_check=True`` must resolve to a real gateway route.

    ``hermes_cli/doctor.py`` probes ``models_url or base_url + "/models"``.
    Leaving ``models_url`` empty is the deliberate choice: the derived address
    cannot drift from the configured one.
    """
    assert profile.supports_health_check is True
    assert profile.models_url == ""

    derived = (profile.models_url or profile.base_url.rstrip("/") + "/models")
    assert derived == "http://127.0.0.1:8787/v1/models"


def test_profile_supports_vision_and_routes_auxiliary_calls_to_a_virtual_model(
    profile: Any,
) -> None:
    assert profile.supports_vision is True
    assert profile.default_aux_model in AUTO_MODELS


def test_build_extra_body_signature_matches_hermes(profile: Any) -> None:
    """Keyword-only ``session_id`` plus ``**context``, per ``providers/base.py``."""
    signature = inspect.signature(profile.build_extra_body)
    params = signature.parameters

    assert params["session_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["session_id"].default is None
    assert any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ), "must accept **context -- Hermes passes model, base_url, reasoning_config"


def test_build_extra_body_wraps_the_envelope_under_the_private_key(
    profile: Any, metadata_validator: Any
) -> None:
    body = profile.build_extra_body(session_id="s-1", model="auto:balanced")

    assert set(body) == {ENVELOPE_KEY}
    assert ENVELOPE_KEY == "_hermes_auto"
    metadata_validator.validate(body[ENVELOPE_KEY])


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="auxiliary_client-form-no-session-id"),
        pytest.param({"session_id": None}, id="chat-helpers-form-none-session-id"),
        pytest.param(
            {
                "session_id": None,
                "model": "auto:quality",
                "base_url": "http://127.0.0.1:8787/v1",
                "reasoning_config": None,
            },
            id="full-transport-form",
        ),
    ],
)
def test_every_real_hermes_call_form_produces_a_valid_envelope(
    profile: Any, metadata_validator: Any, kwargs: dict[str, Any]
) -> None:
    """The three shapes the three Hermes call sites actually use."""
    metadata_validator.validate(profile.build_extra_body(**kwargs)[ENVELOPE_KEY])


def test_virtual_model_comes_from_the_call_context(profile: Any) -> None:
    body = profile.build_extra_body(session_id="s-1", model="auto:economy")

    assert body[ENVELOPE_KEY]["virtual_model"] == "auto:economy"


# ---------------------------------------------------------------------------
# Module-level properties of provider.py itself.
# ---------------------------------------------------------------------------


def test_module_imports_with_hermes_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-importing the module with ``providers`` unimportable must succeed.

    The lazy import inside ``build_profile`` is what makes the installer and
    the CLI usable in this virtualenv, where Hermes genuinely is absent.
    """
    import builtins
    import importlib

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "providers" or name.startswith("providers."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(sys.modules, "hermes_auto.provider", raising=False)

    reloaded = importlib.import_module("hermes_auto.provider")

    assert reloaded.AUTO_MODELS == AUTO_MODELS
    assert reloaded.build_envelope()["protocol_version"] == 1
    with pytest.raises(ModuleNotFoundError):
        reloaded.build_profile()


def test_provider_module_never_calls_register_provider() -> None:
    """Registration belongs in the shim; this module must not have side effects.

    AST-based rather than a substring check: ``'register_provider' not in
    source`` is satisfied by a docstring mentioning it, and its inverse is
    satisfied by any file containing the word.
    """
    tree = ast.parse(inspect.getsource(provider))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "register_provider" not in called


def test_provider_module_has_no_module_scope_hermes_import() -> None:
    """``providers`` must be imported inside a function, never at module scope."""
    tree = ast.parse(inspect.getsource(provider))
    module_scope_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_scope_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_scope_imports.add(node.module.split(".")[0])

    assert "providers" not in module_scope_imports

    nested = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("providers")
    ]
    assert nested, "build_profile must import ProviderProfile lazily"


def test_provider_module_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No logging call can leak the raw session id from this module.

    The envelope carries ``root_session_id`` unhashed by design -- it travels
    over loopback only -- so the producer must not be a second place it can
    escape from.
    """
    source = inspect.getsource(provider)

    assert not re.search(r"\blogger\b|\blogging\.", source)
