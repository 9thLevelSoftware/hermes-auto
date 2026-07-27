"""Source of the provider module written into ``$HERMES_HOME``.

:data:`SHIM_SOURCE` is a template, not an importable module: it is Python text
that runs inside the *Hermes* interpreter after
:func:`hermes_auto.hermes_shim.installer.install` writes it to disk. Three
values are substituted at write time by :func:`render`, because the shim cannot
read this project's configuration -- it cannot import this project at all.

The constraint the shim lives under is that its import set is the standard
library plus Hermes's own ``providers`` package, nothing else. It executes in an
environment where ``starlette``, ``httpx``, ``uvicorn``, ``jsonschema`` and
``pyyaml`` are absent, and where making them present would be the coupling this
whole design exists to avoid. That is why the envelope logic below is copied
from :func:`hermes_auto.provider.build_envelope` rather than imported. The copy
is deliberate; ``tests/integration/test_shim_installer.py`` compares the two
outputs field by field so the duplication cannot drift silently.
"""

from __future__ import annotations

from hermes_auto.provider import DEFAULT_BASE_URL, TOKEN_ENV_VAR
from hermes_auto.version import __version__

__all__ = ["MARKER", "SHIM_SOURCE", "render"]

#: First line of every file this project writes into ``$HERMES_HOME``. The
#: installer treats its presence as proof of ownership: a file that starts with
#: this line may be overwritten or removed, and a file that does not, never is.
#: Bump the version suffix only if the ownership contract itself changes --
#: routine edits to the shim body must keep it stable, or an upgrade would
#: refuse to overwrite the file the previous version installed.
MARKER: str = "# hermes-auto-router provider shim v1 -- managed file, do not edit"

SHIM_SOURCE: str = MARKER + '''
#
# Written by hermes-auto-router into
#   $HERMES_HOME/plugins/model-providers/hermes-auto/__init__.py
# which is the directory Hermes scans for user-supplied model providers
# (providers/__init__.py, _discover_providers). Installing the router with pip
# does NOT register this provider -- model providers are found by directory
# scan, not by entry point -- so this file is the registration.
#
# It runs inside the HERMES interpreter, not the router's. It therefore imports
# the standard library and Hermes's own `providers` package, and nothing else.
# The envelope construction below is DELIBERATELY DUPLICATED from the router's
# provider module rather than imported: importing the router here would pull
# starlette, httpx, uvicorn, jsonschema and pyyaml into the Hermes environment,
# which is the exact coupling this design prevents. A drift test in the
# router's suite asserts the two envelopes stay identical.
#
# Edits here are lost on the next `install()`.

import uuid

from providers import register_provider
from providers.base import ProviderProfile

# Substituted at write time -- the shim cannot read the router's config file.
BASE_URL = "__HERMES_AUTO_BASE_URL__"
TOKEN_ENV_VAR = "__HERMES_AUTO_TOKEN_ENV_VAR__"
PLUGIN_VERSION = "__HERMES_AUTO_PLUGIN_VERSION__"

PROTOCOL_VERSION = 1
DEFAULT_VIRTUAL_MODEL = "auto:balanced"
MAX_FIELD_LENGTH = 256
AUTO_MODELS = ("auto:quality", "auto:balanced", "auto:economy", "auto:session")


class HermesAutoProfile(ProviderProfile):
    """Subclassed for exactly one reason: to override build_extra_body.

    ProviderProfile is a @dataclass. Declarative values belong in the
    constructor call at the bottom of this file, not in class attributes here.
    """

    def build_extra_body(self, *, session_id=None, **context):
        """Attach the private routing envelope to the outgoing request body.

        Hermes reaches this from three call sites. agent/auxiliary_client.py
        passes NO session_id at all (compression, vision, title generation,
        web extraction); agent/chat_completion_helpers.py passes a possibly
        -None one. The gateway validates root_session_id against a schema
        pinning it to minLength 1, so `session_id or ""` would turn every one
        of those auxiliary calls into a 400. Synthesize a unique non-empty id
        instead.

        The two values that come from Hermes are clamped to the schema's
        length cap: emitting a value the router's own schema rejects would
        surface as a request failure rather than a routing decision.
        """
        root_session_id = session_id or ("no-session-" + uuid.uuid4().hex)
        model = context.get("model") or DEFAULT_VIRTUAL_MODEL
        return {
            "_hermes_auto": {
                "protocol_version": PROTOCOL_VERSION,
                "root_session_id": str(root_session_id)[:MAX_FIELD_LENGTH],
                "virtual_model": str(model)[:MAX_FIELD_LENGTH],
                "plugin_version": PLUGIN_VERSION,
            }
        }


# Instantiate with keyword arguments, then register the INSTANCE. Bare class
# attributes on the subclass populate no dataclass field and register nothing;
# they also make the constructor raise, since `name` has no default.
auto_router_profile = HermesAutoProfile(
    name="hermes-auto",
    display_name="Hermes Auto Router",
    description="Capability-, cost-, cache-, and outcome-aware model routing",
    api_mode="chat_completions",
    base_url=BASE_URL,
    env_vars=(TOKEN_ENV_VAR,),
    fallback_models=AUTO_MODELS,
    default_aux_model=DEFAULT_VIRTUAL_MODEL,
    supports_vision=True,
    supports_health_check=True,
)

register_provider(auto_router_profile)
'''


def render(
    source: str = SHIM_SOURCE,
    *,
    base_url: str = DEFAULT_BASE_URL,
    token_env_var: str = TOKEN_ENV_VAR,
    plugin_version: str = __version__,
) -> str:
    """Return *source* with its three placeholders filled in.

    Takes the template as an argument rather than reaching for the module
    global, so the installer names :data:`SHIM_SOURCE` at its call site -- what
    gets written into ``$HERMES_HOME`` is visible there instead of hidden in a
    default -- and so the missing-placeholder guard below can be tested against
    a deliberately broken template without monkeypatching a module global.

    Values are inserted as ``repr()`` over the whole quoted placeholder
    literal, so a value containing a quote, a backslash or a newline produces a
    correct literal rather than a syntax error or an injection.

    Raises:
        ValueError: if a placeholder is missing from the template, which would
            mean an edit to :data:`SHIM_SOURCE` silently dropped a substitution
            and left the literal ``__HERMES_AUTO_*__`` text in the installed
            file.
    """
    values = {
        "__HERMES_AUTO_BASE_URL__": base_url,
        "__HERMES_AUTO_TOKEN_ENV_VAR__": token_env_var,
        "__HERMES_AUTO_PLUGIN_VERSION__": plugin_version,
    }
    for placeholder, value in values.items():
        quoted = f'"{placeholder}"'
        if quoted not in source:
            raise ValueError(f"shim template is missing placeholder {placeholder}")
        source = source.replace(quoted, repr(str(value)))
    return source
