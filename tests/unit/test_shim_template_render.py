"""``render()`` substitutes placeholders in one pass, not three sequential ones.

The shim is a Python module written into ``$HERMES_HOME`` and imported by Hermes
at startup. A shim that does not parse is not a bad value in a config field; it
is Hermes failing to start, with a ``SyntaxError`` naming a generated file the
operator never wrote.

``render()`` used a sequential ``str.replace()`` per placeholder, which makes the
result depend on substitution *order*: a value inserted by an earlier pass is
still part of the string the next pass scans, so a value that happens to contain
a later placeholder's quoted form gets rewritten inside the literal that was
supposed to contain it.

This is robustness, not injection. Values are inserted as ``repr()`` of the whole
quoted placeholder, so the quoting is correct for any value and code execution
is not reachable this way; and all three inputs are operator-configured rather
than attacker-supplied. What is reachable is a mangled literal and a shim that
will not import -- and the mangling is silent at render time, so the first symptom
is at Hermes startup, far from the cause.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from hermes_auto.hermes_shim.template import (
    SHIM_SOURCE,
    TOKEN_ENV_VAR,
    render,
)

#: The value that breaks a sequential replace: it *is* the quoted form of the
#: placeholder that gets substituted after ``base_url``.
COLLIDING_BASE_URL = '"__HERMES_AUTO_TOKEN_ENV_VAR__"'


@pytest.fixture
def exec_rendered(monkeypatch: pytest.MonkeyPatch):
    """Compile and execute a rendered shim, returning its module namespace.

    Compiling is the assertion that matters -- a mangled literal is a
    ``SyntaxError`` here -- and executing lets the test read back what the
    constants actually bound to.

    ``providers`` is stubbed rather than imported: the shim registers itself
    with Hermes at import, and nothing about template substitution needs the
    real package. ``monkeypatch.setitem`` restores ``sys.modules`` afterwards.
    """

    def run(**kwargs: str) -> dict[str, Any]:
        providers_module = types.ModuleType("providers")
        providers_module.register_provider = lambda profile: None  # type: ignore[attr-defined]
        base_module = types.ModuleType("providers.base")
        base_module.ProviderProfile = type(  # type: ignore[attr-defined]
            "StubProviderProfile",
            (),
            {"__init__": lambda self, **kw: self.__dict__.update(kw)},
        )
        providers_module.base = base_module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "providers", providers_module)
        monkeypatch.setitem(sys.modules, "providers.base", base_module)

        namespace: dict[str, Any] = {"__name__": "_shim_under_test"}
        source = render(SHIM_SOURCE, **kwargs)
        exec(compile(source, "<shim>", "exec"), namespace)  # noqa: S102
        return namespace

    return run


def test_a_value_containing_a_later_placeholder_still_renders(exec_rendered) -> None:
    """The reproduction, minimised.

    With a sequential replace this produced ``BASE_URL = ''HERMES_AUTO_ROUTER_TOKEN''``
    -- two adjacent string literals with no operator between them -- and the
    module failed to compile.
    """
    namespace = exec_rendered(base_url=COLLIDING_BASE_URL)

    assert namespace["BASE_URL"] == COLLIDING_BASE_URL
    # The later placeholder must still have been substituted with its own value,
    # not consumed while rewriting the earlier one.
    assert namespace["TOKEN_ENV_VAR"] == TOKEN_ENV_VAR


def test_every_value_may_contain_every_placeholder(exec_rendered) -> None:
    """Order-independence stated as a property rather than as one example.

    Each of the three values is set to the quoted form of a *different*
    placeholder, so any surviving order dependence -- in either direction --
    corrupts at least one of them.
    """
    values = {
        "base_url": '"__HERMES_AUTO_TOKEN_ENV_VAR__"',
        "token_env_var": '"__HERMES_AUTO_PLUGIN_VERSION__"',
        "plugin_version": '"__HERMES_AUTO_BASE_URL__"',
    }
    namespace = exec_rendered(**values)

    assert namespace["BASE_URL"] == values["base_url"]
    assert namespace["TOKEN_ENV_VAR"] == values["token_env_var"]
    assert namespace["PLUGIN_VERSION"] == values["plugin_version"]


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.com/v1",
        # Quote, backslash and newline: the cases repr() exists to handle, and
        # the ones a naive f-string substitution would break.
        'has "double" quotes',
        "has 'single' quotes",
        "has\\a\\backslash",
        "has\na\nnewline",
        "has\ttab and \x00 nul",
        # Non-ASCII must survive a round trip through repr() unharmed.
        "unicode: é中文\U0001f600",
        "",
    ],
)
def test_awkward_values_round_trip_exactly(value: str, exec_rendered) -> None:
    """Whatever went in is what the shim binds, byte for byte."""
    namespace = exec_rendered(base_url=value)
    assert namespace["BASE_URL"] == value


def test_a_missing_placeholder_is_still_an_error() -> None:
    """The guard that catches an edit to SHIM_SOURCE dropping a substitution.

    Rewriting the substitution must not lose this: a template missing a
    placeholder would otherwise render with the literal ``__HERMES_AUTO_*__``
    text still in it, and the shim would ship pointing at a base URL that is not
    a URL.
    """
    with pytest.raises(ValueError, match="__HERMES_AUTO_TOKEN_ENV_VAR__"):
        render('BASE_URL = "__HERMES_AUTO_BASE_URL__"\n')


def test_substitution_is_single_pass_over_the_real_template() -> None:
    """No ``__HERMES_AUTO_*__`` text survives in a normally-rendered shim."""
    rendered = render(SHIM_SOURCE)
    assert "__HERMES_AUTO_" not in rendered
