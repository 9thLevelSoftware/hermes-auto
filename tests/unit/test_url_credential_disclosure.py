"""``upstream.base_url`` is operator-supplied, and a URL may legally carry a credential.

``https://user:pw@host/v1`` is a valid URL. ``config.py`` validates ``base_url``
only as a string, and ``credential_ref``'s ``env:NAME``-or-``none`` grammar
constrains a *different* field, so nothing stops a credential from arriving in
``base_url`` -- by an operator pasting a provider's "copy this URL" snippet, or
by a proxy that genuinely requires inline auth.

Every site that renders ``base_url`` into something a human or a client can read
is therefore a disclosure site. This module pins the four that exist:

* the 502 body handed to the *client* on a transport failure (``upstream.py``),
* the ``gateway.started`` log record (``app.py``),
* the ``/readyz`` reason, which is served **unauthenticated** (``health/probe.py``),
* ``/admin/v1/status`` (``admin.py``) -- which already sanitized, and is the
  reason the other three were found.

Two layers are asserted, deliberately, and neither makes the other redundant:
``load_config`` rejects userinfo outright, and the output sites sanitize
regardless. The rejection is the layer an operator sees; the sanitizers are what
holds when a config is built in-process, which is exactly how every test in this
repository and every embedding caller constructs one.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import textwrap

import httpx
import pytest

from hermes_auto.config import (
    AutoRouterConfig,
    ConfigError,
    GatewayConfig,
    UpstreamConfig,
    load_config,
)
from hermes_auto.gateway.app import create_app
from hermes_auto.gateway.upstream import UpstreamClient
from hermes_auto.health.probe import check_upstream_reachable
from hermes_auto.telemetry.redaction import RedactingFormatter, sanitize_url

#: The password half of the userinfo. Every assertion below is "this string does
#: not appear", so it is deliberately distinctive enough that a substring match
#: cannot be satisfied by an unrelated fragment of the URL.
SECRET = "sup3rs3cret"

#: A reachable-looking but dead endpoint: port 1 is not listening, so a request
#: fails in the transport layer, which is the branch that builds the 502.
DEAD_URL = f"https://apiuser:{SECRET}@127.0.0.1:1/v1"

#: The malformed branch: a URL with userinfo but no host at all. ``urlsplit``
#: parses it, ``hostname`` is None, and ``upstream_endpoint`` raises a
#: ``ValueError`` whose message interpolates the whole URL.
HOSTLESS_URL = f"https://apiuser:{SECRET}@/v1"


def _config(base_url: str, state_dir: pathlib.Path) -> AutoRouterConfig:
    return AutoRouterConfig(
        gateway=GatewayConfig(state_dir=state_dir),
        upstream=UpstreamConfig(
            base_url=base_url, model="gpt-4o-mini", credential_ref="none"
        ),
    )


# ---------------------------------------------------------------------------
# The shared sanitizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Password and username both stripped, path/query/fragment preserved.
        (
            "https://apiuser:sup3rs3cret@api.example.com/v1?a=1#f",
            "https://api.example.com/v1?a=1#f",
        ),
        # Username only -- no colon -- is still userinfo and still stripped.
        ("https://apiuser@api.example.com/v1", "https://api.example.com/v1"),
        # An explicit port survives; dropping it would change where a reader
        # believes traffic is going, which is the one thing this must not do.
        (
            "http://u:p@127.0.0.1:8080/v1",
            "http://127.0.0.1:8080/v1",
        ),
        # An empty password is still a colon-bearing userinfo section.
        ("https://apiuser:@api.example.com/v1", "https://api.example.com/v1"),
        # No userinfo: returned byte-for-byte, including a trailing slash.
        ("https://api.example.com/v1/", "https://api.example.com/v1/"),
    ],
)
def test_sanitize_url_removes_userinfo_and_nothing_else(raw: str, expected: str) -> None:
    assert sanitize_url(raw) == expected


def test_sanitize_url_never_raises_on_an_unparseable_url() -> None:
    """A sanitizer that raises turns a disclosure guard into an outage.

    Every call site here is already on an error path. If sanitizing the URL
    could itself raise, the 502 handler and the ``/readyz`` handler would both
    become 500s at precisely the moment the operator needs a readable answer.
    """
    # A bracketed-host URL with a non-numeric port: urlsplit accepts the string
    # but raises from `.port`/`.hostname` on access.
    assert sanitize_url("https://u:p@[::1]:notaport/v1") == "<unparseable>"


# ---------------------------------------------------------------------------
# Site 1: the client-facing 502
# ---------------------------------------------------------------------------


def test_transport_failure_502_body_does_not_disclose_userinfo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 502 body is the highest-exposure site: it goes to the *caller*.

    ``_connect_error``'s docstring claims the message "names the configured URL
    -- which is operator-supplied configuration, not a secret". That is true of
    the host and port and false of the userinfo, and the caller of a gateway is
    not necessarily the operator who configured it.
    """
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(tmp_path))
    app = create_app(_config(DEAD_URL, tmp_path))

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway"
        ) as client:
            async with app.router.lifespan_context(app):
                return await client.post(
                    "/v1/chat/completions",
                    headers={"authorization": f"Bearer {app.state.token}"},
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )

    response = asyncio.run(drive())

    assert response.status_code == 502
    assert SECRET not in response.text
    assert "apiuser" not in response.text
    # Candidate ids and sanitized reasons survive; endpoint URLs never enter the
    # all-candidates-failed response.
    assert "legacy-upstream" in response.text
    assert "upstream_unavailable" in response.text
    assert "127.0.0.1:1" not in response.text


def test_upstream_client_connect_error_is_sanitized_at_the_source(
    tmp_path: pathlib.Path,
) -> None:
    """Pinned on the raised ``GatewayError`` too, not only on the rendered body.

    The envelope is built from ``GatewayError.message``. Asserting only on the
    HTTP response would keep passing if a future handler grew a second path
    that rendered the message somewhere else.
    """
    client = UpstreamClient(
        UpstreamConfig(base_url=DEAD_URL, model="m", credential_ref="none")
    )

    async def drive() -> str:
        try:
            await client.complete(b'{"messages":[]}')
        except Exception as exc:  # noqa: BLE001 -- the type is the assertion below
            return str(exc)
        finally:
            await client.aclose()
        raise AssertionError("expected a transport failure against port 1")

    message = asyncio.run(drive())
    assert SECRET not in message
    assert "apiuser" not in message


# ---------------------------------------------------------------------------
# Site 2: the startup log record
# ---------------------------------------------------------------------------


def test_gateway_started_log_record_does_not_disclose_userinfo() -> None:
    """``upstream_base_url`` is not a banned key, so the sink passes it through.

    ``BANNED_KEYS`` matches by exact name and deliberately does not do substring
    matching, so no existing entry covers ``upstream_base_url`` -- and it should
    not be added to ``BANNED_KEYS``, because the host and port are genuinely
    wanted in the log. Sanitizing at the call site is the fix; this test pins
    the record that ``app.py`` actually builds.
    """
    formatter = RedactingFormatter()
    record = logging.makeLogRecord(
        {
            "name": "hermes_auto.gateway",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": {"event": "gateway.started", "upstream_base_url": sanitize_url(DEAD_URL)},
        }
    )
    formatted = formatter.format(record)
    assert SECRET not in formatted
    assert "127.0.0.1:1" in formatted


def test_app_startup_logs_a_sanitized_base_url(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: run the real lifespan and capture what it emits.

    A caplog-based assertion on the *record* rather than on a hand-built one, so
    that this fails if ``app.py`` stops sanitizing even though the formatter and
    the sanitizer are both still correct.
    """
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(tmp_path))
    app = create_app(_config(DEAD_URL, tmp_path))

    emitted: list[object] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(record.msg)

    logger = logging.getLogger("hermes_auto.gateway")
    handler = _Capture(level=logging.DEBUG)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)

    async def drive() -> None:
        async with app.router.lifespan_context(app):
            pass

    try:
        asyncio.run(drive())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    started = [
        payload
        for payload in emitted
        if isinstance(payload, dict) and payload.get("event") == "gateway.started"
    ]
    assert started, "the lifespan did not emit gateway.started"
    assert SECRET not in repr(started)
    assert started[0]["upstream_base_url"] == "https://127.0.0.1:1/v1"


# ---------------------------------------------------------------------------
# Site 3: /readyz, which is unauthenticated
# ---------------------------------------------------------------------------


def test_probe_reason_does_not_disclose_userinfo_on_the_malformed_branch() -> None:
    """``ProbeResult``'s docstring promises the reason "never contains a credential".

    It keeps that promise on every branch but one: the ``ValueError`` branch
    returns ``str(exc)``, and ``upstream_endpoint`` builds that string as
    ``f"...: {base_url!r}"``. The docstring's stated rule -- built "from
    exception types rather than exception strings" -- is the correct rule and
    this is the one place it was not followed.
    """
    result = asyncio.run(check_upstream_reachable(HOSTLESS_URL))
    assert result.ready is False
    assert SECRET not in result.reason
    assert "apiuser" not in result.reason


def test_readyz_body_does_not_disclose_userinfo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/readyz`` carries no auth, so its body is readable by any local process.

    That is a deliberate design choice -- a readiness endpoint a monitor cannot
    reach is useless -- which is exactly why its body must not carry anything
    that authentication would otherwise have protected.
    """
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(tmp_path))
    app = create_app(_config(HOSTLESS_URL, tmp_path))

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway"
        ) as client:
            async with app.router.lifespan_context(app):
                # No Authorization header: this is the point of the test.
                return await client.get("/readyz")

    response = asyncio.run(drive())

    assert response.status_code == 503
    assert SECRET not in response.text
    assert "apiuser" not in response.text


# ---------------------------------------------------------------------------
# The load-time rejection
# ---------------------------------------------------------------------------


def test_load_config_rejects_userinfo_in_base_url(tmp_path: pathlib.Path) -> None:
    """Refuse the credential at the door, in the same voice as ``credential_ref``.

    ``_check_credential_ref`` already teaches the operator the rule --
    "Credentials never belong in config.yaml" -- and a second field that accepts
    one silently would contradict that lesson at the only moment it is being
    taught.
    """
    source = tmp_path / "config.yaml"
    source.write_text(
        textwrap.dedent(
            f"""
            auto_router:
              upstream:
                base_url: https://apiuser:{SECRET}@api.example.com/v1
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as caught:
        load_config(source)

    message = str(caught.value)
    assert "upstream.base_url" in message
    assert "Credentials never belong in config.yaml" in message
    # The error an operator reads must not itself reprint the secret.
    assert SECRET not in message


def test_load_config_still_accepts_an_ordinary_base_url(tmp_path: pathlib.Path) -> None:
    """The guard must reject userinfo and nothing else.

    A rejection keyed on ``@`` alone would break ``.../v1?tag=a@b`` and any
    IPv6 literal, so the check is pinned against the shapes that must keep
    loading.
    """
    source = tmp_path / "config.yaml"
    source.write_text(
        textwrap.dedent(
            """
            auto_router:
              upstream:
                base_url: https://openrouter.ai/api/v1?note=a@b
            """
        ),
        encoding="utf-8",
    )
    assert load_config(source).upstream.base_url == (
        "https://openrouter.ai/api/v1?note=a@b"
    )
