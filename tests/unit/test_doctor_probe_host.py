"""``doctor`` must probe the host the gateway actually bound.

This is the same root cause as a blocker fixed in review cycle 1, reintroduced
at a second call site. ``supervisor._probe_host`` exists precisely because
hardcoding ``127.0.0.1`` orphaned a live gateway: ``gateway/main.py``'s
``require_loopback`` accepts ``localhost`` and ``::1``, and ``bind_socket`` binds
``getaddrinfo(...)[0]``, which for ``localhost`` is ``::1`` first on Windows,
macOS, and any IPv6-enabled Linux.

``supervisor.PROBE_HOST``'s own comment says it is "**Not** the address probes
always use" and is "Kept as a module constant because ``commands.py`` reads it"
-- which is an accurate description of the defect: ``commands.py`` was reading
the *fallback* rather than the *resolution*. With ``gateway.url:
http://localhost:8787`` and a gateway on ``::1``, ``doctor`` probed IPv4 and
reported an upstream failure that was not real.

The consequence is narrower than cycle 1's -- ``doctor`` reports, it does not
delete runtime files -- but a diagnostic that invents failures is worse than no
diagnostic, because the operator's next move is to debug the gateway rather than
the probe.
"""

from __future__ import annotations

import pathlib

import pytest

from hermes_auto import commands, supervisor
from hermes_auto.config import AutoRouterConfig, GatewayConfig, UpstreamConfig


def _running(port: int) -> supervisor.Status:
    return supervisor.Status(
        running=True,
        instance_id="11111111-1111-4111-8111-111111111111",
        port=port,
        pid=4321,
        detail="running",
        kind=supervisor.STATUS_RUNNING,
    )


def _config(url: str, state_dir: pathlib.Path) -> AutoRouterConfig:
    return AutoRouterConfig(
        gateway=GatewayConfig(url=url, state_dir=state_dir),
        upstream=UpstreamConfig(
            base_url="https://api.example.com/v1",
            model="m",
            credential_ref="none",
        ),
    )


def test_probe_host_is_publicly_reachable() -> None:
    """``commands.py`` needs the resolution, so the resolution must be public.

    Reading ``supervisor._probe_host`` across the module boundary would work and
    would be exactly the kind of private coupling that gets "cleaned up" into a
    constant again.
    """
    config = _config("http://localhost:8787", pathlib.Path("."))
    assert supervisor.probe_host(config) == "localhost"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # The regression case: resolves to ::1 first on every IPv6-enabled host.
        ("http://localhost:8787", "localhost"),
        # An explicit IPv6 loopback literal must survive as itself.
        ("http://[::1]:8787", "::1"),
        # An explicit IPv4 literal is already right.
        ("http://127.0.0.1:8787", "127.0.0.1"),
        # Anything non-loopback falls back to the documented default rather
        # than sending a diagnostic probe off-box.
        ("http://example.com:8787", supervisor.PROBE_HOST),
    ],
)
def test_doctor_probes_the_resolved_host(
    url: str,
    expected: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_AUTO_STATE_DIR", str(tmp_path))
    config = _config(url, tmp_path)

    captured: list[str] = []

    def fake_get(target: str, timeout: float = 3.0) -> tuple[int, bytes]:
        captured.append(target)
        return 200, b'{"detail": "upstream 1.2.3.4:443 accepted a TCP connection"}'

    monkeypatch.setattr(commands, "_http_get", fake_get)
    monkeypatch.setattr(supervisor, "status", lambda _config: _running(8787))

    commands.run_doctor(config=config)

    assert captured, "doctor did not probe /readyz at all"
    probed = captured[0]
    assert probed.endswith("/readyz")
    # An IPv6 literal must be bracketed or the URL is unparseable; asserting on
    # the authority rather than on a bare substring is what catches that.
    assert supervisor.authority(expected, 8787) in probed


def test_probe_host_of_an_unresolved_config_falls_back_rather_than_raising() -> None:
    """``run_doctor`` reaches this with ``None`` when config could not be loaded.

    ``run_doctor`` is documented to run every check and never raise for a
    failure, and it calls this outside the try that guards ``supervisor.status``.
    An ``AttributeError`` here would abort the whole report -- including the
    checks that would have told the operator *why* the config failed to load.
    """
    assert supervisor.probe_host(None) == supervisor.PROBE_HOST


def test_authority_brackets_an_ipv6_literal() -> None:
    """``http://::1:8787/readyz`` is not a URL. RFC 3986 requires the brackets."""
    assert supervisor.authority("::1", 8787) == "[::1]:8787"
    assert supervisor.authority("127.0.0.1", 8787) == "127.0.0.1:8787"
