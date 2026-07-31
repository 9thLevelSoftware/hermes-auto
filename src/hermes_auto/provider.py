"""The ``hermes-auto`` Hermes model-provider profile (R1).

Two verified facts about Hermes govern this module, and ``design.md`` §5.1 is
wrong about both. They were re-checked against the local Hermes 0.19.0 checkout
before this file was written:

1. ``providers.base.ProviderProfile`` is a ``@dataclass``
   (``providers/base.py:38``). A subclass carrying bare class attributes --
   the form ``design.md`` §5.1 depicts -- never populates a dataclass instance.
   Worse, it cannot even be constructed: ``name`` has no default, so
   ``HermesAutoProfile()`` raises ``TypeError``. Subclass only to override
   methods, then instantiate with keyword arguments.
2. Model providers are discovered by directory scan of
   ``$HERMES_HOME/plugins/model-providers/`` (``providers/__init__.py``
   ``_discover_providers``), not by pip entry points. Nothing in this module
   registers anything. Registration happens in the shim that
   :mod:`hermes_auto.hermes_shim.installer` writes into that directory, which
   runs inside the Hermes environment rather than this one.

``build_profile`` therefore imports ``ProviderProfile`` lazily, inside the
function. Hermes is a source checkout on this machine, not an installed
distribution, so ``import providers`` fails from the router's virtualenv;
importing at module scope would make this module unimportable in the very
environment it ships to. Callers that need a real profile object must run where
``providers`` resolves -- a process whose working directory is the Hermes
checkout root, or the Hermes interpreter itself.

:func:`build_envelope` is the one piece of logic the shim duplicates, so it is
kept separate, pure, and free of any dependency: it is the drift-test surface.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from hermes_auto.config import AutoRouterConfig, load_config
from hermes_auto.version import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from providers.base import ProviderProfile

__all__ = [
    "AUTO_MODELS",
    "DEFAULT_BASE_URL",
    "DEFAULT_VIRTUAL_MODEL",
    "ENVELOPE_KEY",
    "MAX_FIELD_LENGTH",
    "PROTOCOL_VERSION",
    "PROVIDER_NAME",
    "TOKEN_ENV_VAR",
    "build_envelope",
    "build_profile",
    "gateway_base_url",
]

#: The virtual models this provider offers. Hermes shows these in its
#: ``/model`` picker when a live catalog fetch fails, and accepts them as
#: ``model`` values regardless.
AUTO_MODELS: tuple[str, ...] = (
    "auto",
    "auto:balanced",
    "auto:quality",
    "auto:economy",
    "auto:session",
)

PROVIDER_NAME: str = "hermes-auto"

#: The private request-body channel between this plugin and the local gateway.
#: The gateway pops it before forwarding upstream, so no provider ever sees it.
ENVELOPE_KEY: str = "_hermes_auto"

#: Pinned by ``routing/hermes-auto-metadata.v1``. A mismatched plugin and
#: gateway disagree about the meaning of every other field, so the gateway
#: rejects any other value rather than best-effort parsing it.
PROTOCOL_VERSION: int = 1

#: Every string in the envelope is capped at 256 characters by the schema.
#: Two of the three come from Hermes rather than from us, so they are clamped
#: here: emitting a value our own frozen schema rejects would turn a long
#: session id into a 400 on every request instead of a routing decision.
MAX_FIELD_LENGTH: int = 256

#: Used when Hermes supplies no ``model`` in the call context.
DEFAULT_VIRTUAL_MODEL: str = "auto"

#: Declared to Hermes so ``hermes doctor`` and the credential prompts know
#: which variable carries the gateway's bearer token.
TOKEN_ENV_VAR: str = "HERMES_AUTO_ROUTER_TOKEN"

#: Used when no configuration file overrides the focused gateway address.
DEFAULT_BASE_URL: str = "http://127.0.0.1:8787/v1"


def gateway_base_url(config: AutoRouterConfig | None = None) -> str:
    """Return the OpenAI-compatible base URL Hermes should dial.

    Derived from ``auto_router.gateway.url`` -- the address Hermes talks to --
    with the ``/v1`` prefix the
    OpenAI client appends its paths under. ``gateway.port`` is deliberately not
    consulted: plan 02-01 derives the port *from* the URL, so the URL is the
    authority and reading the port back could produce an address that disagrees
    with the one the user configured.

    Configuration errors are not swallowed. A missing file is not an error and
    yields the built-in default; a present-but-broken file raises
    ``ConfigError``, because a provider silently falling back to a default
    endpoint would send traffic somewhere the operator did not choose.
    """
    resolved = config if config is not None else load_config(None)
    return resolved.gateway.url.rstrip("/") + "/v1"


def build_envelope(
    *,
    session_id: str | None = None,
    virtual_model: str | None = None,
    plugin_version: str = __version__,
) -> dict[str, Any]:
    """Build the ``_hermes_auto`` envelope body for one request.

    Kept pure and dependency-free because
    :data:`hermes_auto.hermes_shim.template.SHIM_SOURCE` reimplements it
    verbatim -- it cannot import this package -- and a drift test compares the
    two outputs field by field.

    The ``session_id`` fallback is load-bearing, not cosmetic. Hermes reaches
    ``build_extra_body`` from three call sites, and one of them
    (``agent/auxiliary_client.py:7006``) passes **no** ``session_id`` at all;
    ``agent/chat_completion_helpers.py:2097`` passes
    ``getattr(agent, "session_id", None)``, which is frequently ``None``.
    ``root_session_id`` is ``minLength: 1`` in the provider-envelope schema and the
    gateway validates the envelope inline, so emitting ``session_id or ""``
    would return 400 for every compression, vision, title-generation and
    web-extraction call. A synthesized unique id keeps those calls working and keeps them
    distinguishable from one another downstream.
    """
    root_session_id = session_id or f"no-session-{uuid.uuid4().hex}"
    model = virtual_model or DEFAULT_VIRTUAL_MODEL
    return {
        "protocol_version": PROTOCOL_VERSION,
        "root_session_id": str(root_session_id)[:MAX_FIELD_LENGTH],
        "virtual_model": str(model)[:MAX_FIELD_LENGTH],
        "plugin_version": plugin_version,
    }


def build_profile(
    base_url: str | None = None,
    *,
    plugin_version: str = __version__,
) -> ProviderProfile:
    """Construct the ``hermes-auto`` profile instance.

    Raises ``ModuleNotFoundError`` when Hermes's ``providers`` package is not
    importable. That is deliberate and not caught: a ``ProviderProfile`` has no
    meaning outside Hermes, and returning a lookalike would let a caller
    believe it had a registrable profile when it had a stand-in. The module
    itself stays importable either way -- the import lives here, not at module
    scope -- which is what lets the installer and the CLI run in the router's
    own environment.
    """
    from providers.base import ProviderProfile as _ProviderProfile

    class HermesAutoProfile(_ProviderProfile):
        """Subclassed for one reason: to override ``build_extra_body``.

        Every declarative field is passed as a keyword argument to the
        constructor below, because the base class is a dataclass.
        """

        def build_extra_body(
            self, *, session_id: str | None = None, **context: Any
        ) -> dict[str, Any]:
            """Inject the routing envelope into the request body.

            Signature matches ``providers/base.py:119`` exactly. ``context``
            carries ``model``, ``base_url`` and ``reasoning_config`` from all
            three Hermes call sites; only ``model`` is read here.
            """
            return {
                ENVELOPE_KEY: build_envelope(
                    session_id=session_id,
                    virtual_model=context.get("model"),
                    plugin_version=plugin_version,
                )
            }

    return HermesAutoProfile(
        name=PROVIDER_NAME,
        display_name="Hermes Auto Router",
        description="Deterministic capability- and complexity-aware /model auto routing",
        api_mode="chat_completions",
        base_url=base_url if base_url is not None else gateway_base_url(),
        env_vars=(TOKEN_ENV_VAR,),
        fallback_models=AUTO_MODELS,
        default_aux_model=DEFAULT_VIRTUAL_MODEL,
        supports_vision=True,
        # Left True on purpose. ``hermes doctor`` probes ``base_url + /models``
        # with ``Authorization: Bearer $HERMES_AUTO_ROUTER_TOKEN``
        # (``hermes_cli/doctor.py:2128-2176``) and skips the probe entirely
        # when the variable is unset, so it never produces a spurious failure
        # for a user who has not configured the router. When the variable *is*
        # set, the gateway serves exactly that endpoint with exactly that auth
        # (R3), and "gateway not running" is precisely the diagnosis a user
        # wants doctor to report. ``models_url`` is left empty so the probe
        # derives ``{base_url}/models`` -- setting it explicitly would pin a
        # second copy of the address that could drift from the configured one.
        supports_health_check=True,
    )
