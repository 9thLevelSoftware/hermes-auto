"""The reachability check behind ``/readyz``, kept out of the app on purpose.

Liveness and readiness answer different questions and a gateway that aliases
them is useless for both. ``/healthz`` asks "is this process running and is it
the one you think it is" -- it must stay cheap, dependency-free, and true even
when every upstream is down, because supervision uses it to decide whether to
restart. ``/readyz`` asks "can this process do useful work right now", which
necessarily involves something outside the process and can therefore be slow,
flaky, or false through no fault of the gateway. Restarting on a failed
``/readyz`` would restart the gateway every time the provider had a bad minute.

**The probe is a TCP connect, not an HTTP request.** Three reasons, in order of
weight:

1. It costs the provider nothing. ``GET /v1/models`` against a paid endpoint is
   a billable API call, and ``/readyz`` is the kind of endpoint an operator
   points a monitor at every ten seconds.
2. It needs no credential. A readiness probe that fails when the API key is
   wrong is reporting an authentication problem as an availability problem, and
   the credential is deliberately confined to ``gateway/upstream.py``.
3. Not every OpenAI-compatible server implements ``/models``. A 404 there is not
   an outage, but a probe built on it cannot tell the difference.

What it consequently does *not* prove: that the credential is valid, that the
model exists, or that the provider will accept the request. Those are answered by
the first real request, and the honest place to surface them is that request's
own error. This module states its own limits rather than overclaiming, which is
the house rule the schema descriptions follow too.

Phase 6 extends readiness with candidate health (design.md 11.1). It extends this
module; it does not reach into ``gateway/app.py``.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from dataclasses import dataclass

from ..telemetry.redaction import sanitize_url

__all__ = ["ProbeResult", "check_upstream_reachable", "upstream_endpoint"]

#: Default ceiling for a readiness probe. Short: a readiness endpoint that hangs
#: is worse than one that reports "not ready", because a monitor's own timeout
#: then attributes the stall to the gateway.
DEFAULT_PROBE_TIMEOUT_SECONDS = 2.0

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one reachability probe.

    ``reason`` is written for a human reading ``/readyz`` output or ``doctor``,
    and never contains a credential. Two rules keep that true, and the second
    exists because the first was not sufficient on its own:

    * **No foreign exception strings.** An ``httpx``/``socket`` exception string
      can contain a full URL, and a full URL can contain an embedded
      credential, so those exceptions contribute their *type* and nothing else.
    * **URLs are sanitized, not trusted for being configuration.** The reason is
      built from the configured URL's host and port. Being operator-supplied
      does not make a URL non-secret -- ``https://user:pw@host/v1`` is legal --
      and ``/readyz`` is served **unauthenticated**, so its body is readable by
      any local process. The one branch that reports a *malformed* URL routes it
      through :func:`~hermes_auto.telemetry.redaction.sanitize_url` first.
    """

    ready: bool
    reason: str

    def __iter__(self):
        """Unpack as ``(ready, reason)``."""
        return iter((self.ready, self.reason))


def upstream_endpoint(base_url: str) -> tuple[str, int]:
    """Return the ``(host, port)`` a connection to *base_url* would open.

    Raises:
        ValueError: *base_url* has no host, or a port that is not a number in
            range. Both are configuration errors worth failing loudly on.

    The raised message quotes the **sanitized** URL. This function is exported,
    its ValueError is rendered verbatim into the unauthenticated ``/readyz``
    body, and a URL malformed enough to reach these branches can still carry a
    well-formed userinfo section -- ``https://user:pw@/v1`` has no host and a
    complete credential.
    """
    parsed = urllib.parse.urlsplit(base_url)
    try:
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError(
            f"upstream base_url is not a parseable URL: {sanitize_url(base_url)!r}"
        ) from exc
    if not host:
        raise ValueError(
            f"upstream base_url has no host: {sanitize_url(base_url)!r}"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            f"upstream base_url has an invalid port: {sanitize_url(base_url)!r}"
        ) from exc
    if port is None:
        port = _DEFAULT_PORTS.get(parsed.scheme.lower(), 80)
    return host, port


async def check_upstream_reachable(
    base_url: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Open and immediately close a TCP connection to the upstream.

    Returns rather than raises for every failure mode. A readiness probe that
    raises turns into a 500, and a 500 from ``/readyz`` is indistinguishable from
    the gateway itself being broken -- which is the one thing readiness must not
    claim on the upstream's behalf.
    """
    try:
        host, port = upstream_endpoint(base_url)
    except ValueError as exc:
        return ProbeResult(False, str(exc))

    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            False, f"upstream {host}:{port} did not accept a connection within {timeout}s"
        )
    except (OSError, ConnectionError) as exc:
        return ProbeResult(
            False, f"upstream {host}:{port} is not accepting connections ({type(exc).__name__})"
        )
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                # The peer closing first during our own close is not a probe
                # failure; the connection succeeded, which is what was measured.
                pass

    return ProbeResult(True, f"upstream {host}:{port} accepted a TCP connection")
