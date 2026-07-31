"""The fail-open authentication path in ``gateway/ingress.py``, and its fix.

Found by plan 02-06 while closing the identical defect in ``gateway/admin.py``,
and fixed here under an explicit scope extension because both plans that owned
``ingress.py`` had completed.

The defect: ``hmac.compare_digest(b"", b"")`` returns **True**. So

    compare_token(supplied, expected_token or "")

admits a caller who sent no ``Authorization`` header to an app that has no token
configured -- the exact opposite of the comment sitting directly above it, which
says *"a gateway with no token configured must reject every request"*.

Not reachable in production: ``gateway/app.py``'s lifespan always mints a token
before the first request. But ``create_app()`` used without a lifespan -- which
is what a unit test, an embedding host, or an ASGI mount does -- authenticates
unauthenticated requests. "Only reachable through a test harness" is not a
defence for an auth bypass; the harness is how the next person convinces
themselves the auth works.

The fix mirrors ``admin.py``: branch explicitly on an absent expected token and
deny unconditionally, while still paying a decoy comparison so that "no token
configured" and "wrong token" do not separate under timing analysis.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from hermes_auto.gateway import ingress
from hermes_auto.gateway.errors import GatewayError


class _FakeState:
    """Stands in for ``app.state`` before a lifespan has run."""

    def __init__(self, token: object) -> None:
        if token is not _ABSENT:
            self.token = token


_ABSENT = object()


class _FakeApp:
    def __init__(self, token: object) -> None:
        self.state = _FakeState(token)


class _FakeRequest:
    """The two attributes ``authenticate`` touches, and nothing else."""

    def __init__(self, token: object, header: str | None = None) -> None:
        self.app = _FakeApp(token)
        self.headers = {} if header is None else {"authorization": header}


def _denied(request: _FakeRequest) -> bool:
    try:
        ingress.authenticate(request)
    except GatewayError:
        return True
    return False


# ---------------------------------------------------------------------------
# The regression: these fail against the pre-fix implementation
# ---------------------------------------------------------------------------


def test_no_token_configured_rejects_a_request_with_no_credential() -> None:
    """The bypass itself.

    Pre-fix this returns without raising, because ``compare_token("", "")`` is
    ``True``. It is the single assertion that separates the broken and fixed
    implementations, so it is written against the plainest possible request:
    no token on the app, no header on the caller.
    """
    assert _denied(_FakeRequest(None)), (
        "a gateway with no token configured admitted a request that carried no "
        "credential at all -- compare_digest(b'', b'') is True"
    )


@pytest.mark.parametrize(
    "configured",
    [None, "", _ABSENT],
    ids=["token-is-none", "token-is-empty-string", "token-attribute-absent"],
)
def test_every_shape_of_absent_token_denies_every_caller(configured: object) -> None:
    """An unusable expected token denies, whatever the caller sends.

    Three shapes reach ``authenticate`` in practice: ``None`` (``create_app``
    initialises ``app.state.token = None``), ``""`` (a truncated token file read
    by a caller that passes it in explicitly), and a missing attribute (a bare
    ASGI mount). None of them may authenticate anyone.
    """
    for header in (None, "Bearer ", "Bearer whatever", "Basic abc", "garbage"):
        request = _FakeRequest(configured, header)
        assert _denied(request), (
            f"expected_token={configured!r} admitted header={header!r}"
        )


def test_a_configured_token_still_works() -> None:
    """The fix must not break the case the code exists for."""
    request = _FakeRequest("s3cret", "Bearer s3cret")
    assert not _denied(request)
    assert _denied(_FakeRequest("s3cret", "Bearer wrong"))
    assert _denied(_FakeRequest("s3cret", None))


def test_the_explicit_expected_token_argument_is_honoured() -> None:
    """``authenticate(request, token)`` bypasses ``app.state`` -- both ways."""
    with pytest.raises(GatewayError):
        ingress.authenticate(_FakeRequest("ignored", "Bearer ignored"), "")
    ingress.authenticate(_FakeRequest(None, "Bearer real"), "real")


# ---------------------------------------------------------------------------
# The timing oracle, proved by execution rather than counted in the source
# ---------------------------------------------------------------------------


@pytest.fixture
def comparisons(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every ``compare_token`` call ``authenticate`` actually makes.

    ``authenticate`` reads ``compare_token`` as a module global, so replacing it
    on the module is enough. The real implementation still runs underneath, so
    the allow path keeps working and this observes rather than stubs.
    """
    recorded: list[tuple[str, str]] = []
    real = ingress.compare_token

    def spy(supplied: str, expected: str) -> bool:
        recorded.append((supplied, expected))
        return real(supplied, expected)

    monkeypatch.setattr(ingress, "compare_token", spy)
    return recorded


#: The four ways a request is refused. Every one of them must reach the
#: comparison, because the oracle is the *difference* between them.
_DENIAL_PATHS = [
    pytest.param("s3cret", None, "", id="no-header"),
    pytest.param("s3cret", "Basic abc", "", id="malformed-header"),
    pytest.param("s3cret", "Bearer wrong", "wrong", id="wrong-token"),
    pytest.param(None, "Bearer whatever", "whatever", id="no-expected-token"),
]


@pytest.mark.parametrize("configured,header,supplied", _DENIAL_PATHS)
def test_every_denial_path_executes_exactly_one_comparison(
    configured: object,
    header: str | None,
    supplied: str,
    comparisons: list[tuple[str, str]],
) -> None:
    """Absent, malformed, wrong and unconfigured must all cost the same.

    This is the property the module docstring exists for, and counting
    ``ast.Call`` nodes does not assert it. The mutation it has to catch is an
    early return added *above* the comparisons::

        supplied = _bearer(request.headers.get("authorization"))
        if not supplied:
            raise _invalid_api_key()

    Both original ``compare_token`` calls survive that edit, so a source-level
    count passes while the no-header and malformed-header paths return without
    comparing anything -- handing a local process exactly the timing oracle
    ``ingress.py`` is written to deny it. Only running the function can tell.

    Exactly one, not at least one: a path that compares twice is as
    distinguishable as one that compares zero times.
    """
    assert _denied(_FakeRequest(configured, header)), "this path must deny"

    assert len(comparisons) == 1, (
        f"expected exactly one compare_token call on the "
        f"configured={configured!r} header={header!r} path; got "
        f"{len(comparisons)}. Zero means an early return skipped the "
        f"comparison and this path is now measurably faster than the others."
    )
    assert comparisons[0][0] == supplied, (
        "the comparison must be against what the caller actually sent"
    )


def test_the_allow_path_executes_exactly_one_comparison(
    comparisons: list[tuple[str, str]],
) -> None:
    """The success path is the baseline every denial is measured against."""
    assert not _denied(_FakeRequest("s3cret", "Bearer s3cret"))

    assert comparisons == [("s3cret", "s3cret")], comparisons


def test_the_absent_token_path_compares_against_the_decoy_not_the_empty_string(
    comparisons: list[tuple[str, str]],
) -> None:
    """The decoy has to be a real operand, or the comparison is free.

    ``compare_digest`` is constant-time in the length of its inputs. Paying a
    comparison against ``""`` would execute a call and still return faster than
    the wrong-token path, which reinstates the oracle one level down.
    """
    assert _denied(_FakeRequest(None, "Bearer whatever"))

    (_, expected), = comparisons
    assert expected == ingress._DECOY_TOKEN
    assert len(expected) >= 32, expected


def test_the_decoy_token_is_random_and_not_a_literal() -> None:
    """A hardcoded decoy would be a token an attacker could simply send."""
    decoy = getattr(ingress, "_DECOY_TOKEN", None)
    assert isinstance(decoy, str) and len(decoy) >= 32, (
        "ingress needs a high-entropy decoy token for the absent-token path"
    )
    source = pathlib.Path(inspect.getfile(ingress)).read_text(encoding="utf-8")
    assert decoy not in source, "the decoy token must be generated, not literal"


def test_no_bare_or_empty_string_fallback_remains() -> None:
    """The exact construct that caused the bug is gone.

    ``expected_token or ""`` is the defect: it converts "no token" into "the
    empty token", which ``compare_digest`` then matches against an absent
    credential.
    """
    body = inspect.getsource(ingress.authenticate)
    assert 'expected_token or ""' not in body
    assert "expected_token or ''" not in body
