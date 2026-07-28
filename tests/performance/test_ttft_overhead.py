"""The TTFT gate: added time-to-first-token, measured against a delayed upstream.

``design.md`` §15.4 allows **under 1% or 100 ms p99** of added TTFT overhead, and
PROJECT.md repeats it. This module measures it the only way the number means
anything: against an upstream that takes a realistic time to produce its first
byte.

**Why the delay injection is load-bearing.** Against a localhost mock that
answers in microseconds, the gateway's own cost *is* the whole measurement --
direct TTFT is ~1 ms, relayed is ~4 ms, and a gate expressed as "under 1%" fails
on a router that is objectively instant while a gate expressed as "under 100 ms"
passes on one that is objectively broken. Neither reading measures the thing the
budget exists to bound. ``MockUpstream.set_first_byte_delay(0.3)`` puts a
provider-shaped 300 ms in front of both paths, so the delta is the gateway's
contribution to a realistic total. The measured direct TTFT is asserted to be at
least the injected delay: if the injection silently stopped working, this gate
would go back to measuring localhost speed and would pass forever.

**p99, not p95.** Both ``design.md`` §15.4 and PROJECT.md specify p99, and p95 is
a strictly weaker gate on exactly the tail the budget exists to bound. p95 is
reported alongside as context. At n=30 the nearest-rank p99 *is* the maximum
observed delta, which is the strictest honest reading of the sample -- stated
here rather than left for a reader to discover.

**Both settings of ``strict_validation``.** Plan 02-04 measured the hoisted
request validator in isolation at 29.06 ms mean and kept the default off. This
measures it in the request path, which is the number that actually decides the
default. **This module does not change the default** -- it writes a config file
in a temp directory for the second deployment and nothing else.

**Skipped under ``CI``.** A shared runner's scheduling jitter breaches a 100 ms
tail routinely, and a flaky gate gets deleted rather than fixed. The reason is
recorded in the skip message so a CI reader is not left guessing.

Every measurement is printed. A latency gate that reports only pass/fail is not
evidence.
"""

from __future__ import annotations

import math
import os
import pathlib
import statistics
import time
from collections.abc import Iterator, Sequence

import httpx
import pytest

from tests.differential import harness
from tests.differential.harness import Deployment

pytestmark = [pytest.mark.performance, pytest.mark.slow]

#: Paired samples per configuration. The plan's floor is 30.
ITERATIONS = 30
#: Discarded before measuring: the first request through a fresh connection pays
#: a TCP handshake on both hops, which is a connection cost and not a relay cost.
WARMUP = 3
#: Provider-shaped first-byte delay, injected into the mock.
FIRST_BYTE_DELAY_SECONDS = 0.3
#: design.md 15.4.
BUDGET_SECONDS = 0.100
BUDGET_FRACTION = 0.01

#: Validity precondition, not a relaxation of the gate. Two controls, because one
#: alone misses the failure mode that actually occurs.
#:
#: * The **direct** series never touches the gateway: this process talking to a
#:   thread in this process with a fixed sleep in the middle. Its p99 should sit
#:   within a millisecond of its median. Observed at **2583 ms against a 303 ms
#:   median** on this machine while a sibling plan was starting and stopping
#:   gateway processes continuously.
#: * ``GET /healthz`` on the sidecar returns a two-key JSON object from process
#:   memory. It opens no upstream connection, relays nothing, and reads no file,
#:   so its latency is a pure measurement of *whether the sidecar process is being
#:   scheduled*. This is the control the direct series cannot provide: a starved
#:   sidecar produces a 280 ms relay outlier while the in-process direct series
#:   stays flat, and without this the gate would report that as a relay
#:   regression. A genuinely slow relay does not make ``/healthz`` slow, so this
#:   control cannot mask the defect the gate exists to catch.
#:
#: A measurement this invalid must be reported as invalid rather than as either a
#: pass or a failure, so the gate skips and prints everything.
DIRECT_STABILITY_CEILING_SECONDS = 0.025
HEALTH_STABILITY_CEILING_SECONDS = 0.025

#: The fixture used for every measurement. Ordinary streaming text: the gate is
#: about relay overhead, not about how hard a body is to parse -- and the relay
#: parses nothing, which is the point.
FIXTURE = "text_stream"

#: Paired samples for the large-body run. Fewer than ``ITERATIONS`` because this
#: one informs a recommendation rather than gating a release, and each sample
#: still costs the injected 300 ms twice.
LARGE_BODY_ITERATIONS = 15

#: Roughly the size plan 02-04 measured the hoisted request validator against
#: (155 KB / 161 messages). A small-prompt measurement says nothing useful about
#: ``strict_validation``: the validator walks the ``messages`` array against a
#: recursive schema, so its cost is a function of exactly the thing a small body
#: does not have. Without a comparable body this module could not honestly claim
#: to be "the number that decides the default".
LARGE_BODY_MESSAGES = 161
LARGE_BODY_CHARS = 950

#: A body that ``openai-chat-request.v1`` rejects (``messages`` is not an array)
#: but that the gateway's own cheap ingress checks accept. With
#: ``strict_validation`` off it is forwarded and the mock answers 200; with it on
#: the gateway answers 400. That difference is the only proof that the flag was
#: actually live in the measured process, rather than merely written into a
#: config file the sidecar might have ignored.
INVALID_BODY: dict = {
    "model": "auto:balanced",
    "messages": "not-a-list",
    "stream": True,
}

CI_SKIP_REASON = (
    "TTFT tail latency is not measurable on a shared CI runner: loop and "
    "scheduling jitter breach a 100 ms p99 routinely, and a gate that flaps "
    "gets deleted rather than fixed. Run locally; the numbers are printed."
)


def percentile(values: Sequence[float], quantile: float) -> float:
    """Nearest-rank percentile. No interpolation: every value returned was observed."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no samples")
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _ttft(
    client: httpx.Client, url: str, headers: dict[str, str], body: dict
) -> float:
    """Seconds from sending the request to the first response byte.

    The body is drained inside the same loop rather than abandoned, so the
    connection returns to the pool reusable and the next iteration measures a
    warm connection like the one before it.
    """
    first: float | None = None
    started = time.perf_counter()
    with client.stream("POST", url, json=body, headers=headers) as response:
        assert response.status_code == 200, response.status_code
        for chunk in response.iter_raw():
            if chunk and first is None:
                first = time.perf_counter() - started
    if first is None:  # pragma: no cover - would mean an empty 200
        raise AssertionError(f"{url} produced no bytes")
    return first


class Measurement:
    """One configuration's paired samples."""

    def __init__(
        self,
        label: str,
        direct: list[float],
        gateway: list[float],
        health: list[float],
    ) -> None:
        self.label = label
        self.direct = direct
        self.gateway = gateway
        self.health = health
        self.deltas = [g - d for d, g in zip(direct, gateway, strict=True)]

    @property
    def p99_delta(self) -> float:
        return percentile(self.deltas, 0.99)

    @property
    def p95_delta(self) -> float:
        return percentile(self.deltas, 0.95)

    @property
    def allowance(self) -> float:
        """design.md 15.4: under 1% **or** 100 ms p99 -- whichever is larger."""
        return max(BUDGET_SECONDS, BUDGET_FRACTION * percentile(self.direct, 0.99))

    @property
    def direct_spread(self) -> float:
        """How far the gateway-free control's own tail sits above its median."""
        return percentile(self.direct, 0.99) - percentile(self.direct, 0.50)

    @property
    def health_spread(self) -> float:
        """How far the sidecar's do-nothing endpoint's tail sits above its median."""
        return percentile(self.health, 0.99) - percentile(self.health, 0.50)

    @property
    def contaminated(self) -> bool:
        """True when machine load, not the relay, dominates this sample."""
        return (
            self.direct_spread > DIRECT_STABILITY_CEILING_SECONDS
            or self.health_spread > HEALTH_STABILITY_CEILING_SECONDS
        )

    def contamination_detail(self) -> str:
        return (
            f"control spreads (p99 - p50): direct "
            f"{self.direct_spread * 1000:.3f} ms (ceiling "
            f"{DIRECT_STABILITY_CEILING_SECONDS * 1000:.0f} ms), sidecar "
            f"/healthz {self.health_spread * 1000:.3f} ms (ceiling "
            f"{HEALTH_STABILITY_CEILING_SECONDS * 1000:.0f} ms)"
        )

    def report(self) -> str:
        lines = [f"", f"=== TTFT overhead: {self.label} ==="]
        lines.append(
            f"n={len(self.deltas)} paired samples, upstream first-byte delay "
            f"{FIRST_BYTE_DELAY_SECONDS * 1000:.0f} ms, fixture {FIXTURE!r}"
        )
        for name, series in (
            ("direct  ", self.direct),
            ("gateway ", self.gateway),
            ("delta   ", self.deltas),
        ):
            lines.append(
                f"  {name} min {min(series) * 1000:8.3f}  "
                f"p50 {percentile(series, 0.50) * 1000:8.3f}  "
                f"p95 {percentile(series, 0.95) * 1000:8.3f}  "
                f"p99 {percentile(series, 0.99) * 1000:8.3f}  "
                f"max {max(series) * 1000:8.3f}  "
                f"mean {statistics.fmean(series) * 1000:8.3f}   (ms)"
            )
        lines.append(
            "  delta distribution (ms, sorted): "
            + ", ".join(f"{value * 1000:.2f}" for value in sorted(self.deltas))
        )
        share = self.p99_delta / percentile(self.direct, 0.99) * 100
        lines.append(
            f"  p99 delta {self.p99_delta * 1000:.3f} ms = {share:.2f}% of the "
            f"p99 direct TTFT; allowance {self.allowance * 1000:.1f} ms "
            f"(design.md 15.4: under 1% or 100 ms p99)"
        )
        lines.append(
            f"  p95 delta {self.p95_delta * 1000:.3f} ms (context only -- the "
            f"gate is p99)"
        )
        return "\n".join(lines)


def large_body() -> dict:
    """A request the size plan 02-04 validated against, so the numbers compare.

    Synthetic filler only -- no prompt content, nothing that resembles a real
    conversation. The validator's cost depends on the array's shape and size,
    not on what the strings say.
    """
    filler = "synthetic filler token " * (LARGE_BODY_CHARS // 23 + 1)
    return harness.chat_body(
        stream=True,
        messages=[
            {"role": "user" if index % 2 == 0 else "assistant", "content": filler[:LARGE_BODY_CHARS]}
            for index in range(LARGE_BODY_MESSAGES)
        ],
    )


def _measure(
    label: str,
    deployment: Deployment,
    *,
    body: dict | None = None,
    iterations: int = ITERATIONS,
) -> Measurement:
    deployment.upstream.script(FIXTURE)
    deployment.upstream.set_rechunk(None)
    deployment.upstream.set_first_byte_delay(FIRST_BYTE_DELAY_SECONDS)

    if body is None:
        body = harness.chat_body(stream=True)
    gateway_headers = {"Authorization": f"Bearer {deployment.token}"}

    for _ in range(WARMUP):
        _ttft(deployment.direct_client, deployment.upstream_chat_url, {}, body)
        _ttft(
            deployment.gateway_client,
            deployment.gateway_chat_url,
            gateway_headers,
            body,
        )

    health_url = f"{deployment.gateway_url}/healthz"
    direct: list[float] = []
    gateway: list[float] = []
    health: list[float] = []
    for _ in range(iterations):
        # The scheduling control, taken inside the same loop so it samples the
        # same machine conditions the two measured series do.
        started = time.perf_counter()
        deployment.gateway_client.get(health_url)
        health.append(time.perf_counter() - started)
        # Interleaved so a machine-wide slowdown lands on both series rather
        # than on whichever one happened to be running at the time.
        direct.append(
            _ttft(deployment.direct_client, deployment.upstream_chat_url, {}, body)
        )
        gateway.append(
            _ttft(
                deployment.gateway_client,
                deployment.gateway_chat_url,
                gateway_headers,
                body,
            )
        )

    deployment.upstream.set_first_byte_delay(0.0)
    return Measurement(label, direct, gateway, health)


def _strict_probe(deployment: Deployment) -> int:
    """Status the gateway gives an off-schema request. 400 iff strict is live."""
    deployment.upstream.script(FIXTURE)
    deployment.upstream.set_first_byte_delay(0.0)
    response = deployment.gateway_client.post(
        deployment.gateway_chat_url,
        json=INVALID_BODY,
        headers={"Authorization": f"Bearer {deployment.token}"},
    )
    return response.status_code


@pytest.fixture(scope="module")
def measurements(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Measurement]]:
    """Measure both ``strict_validation`` settings, once, and print everything.

    The two deployments run **sequentially and never nested**:
    ``HERMES_AUTO_STATE_DIR`` outranks configuration by design, so a second state
    directory is not reachable from inside one interpreter -- plan 02-07 hit this
    and its first attempt silently wrote into the first install's directory.
    """
    if os.environ.get("CI"):
        pytest.skip(CI_SKIP_REASON)

    body = large_body()
    size = len(str(body))
    results: dict[bool, Measurement] = {}
    large: dict[bool, Measurement] = {}
    probe: dict[bool, int] = {}
    for strict in (False, True):
        root = pathlib.Path(
            tmp_path_factory.mktemp(f"ttft-strict-{'on' if strict else 'off'}")
        )
        label = f"strict_validation={'on' if strict else 'off'}"
        with harness.deployment(root, strict_validation=strict) as deployment:
            assert deployment.strict_validation is strict
            results[strict] = _measure(label, deployment)
            large[strict] = _measure(
                f"{label}, {size // 1024} KB body",
                deployment,
                body=body,
                iterations=LARGE_BODY_ITERATIONS,
            )
            probe[strict] = _strict_probe(deployment)

    for measurement in (results[False], results[True], large[False], large[True]):
        print(measurement.report())
    print(_comparison(results, large, size))
    print(
        f"  strict_validation liveness probe (off-schema request): "
        f"strict off -> HTTP {probe[False]}, strict on -> HTTP {probe[True]}\n"
    )
    STRICT_PROBE.update(probe)
    BODY_BYTES["large"] = size
    yield {
        "off": results[False],
        "on": results[True],
        "large_off": large[False],
        "large_on": large[True],
    }


#: Filled by the fixture; read by the tests below. A module global rather than a
#: fifth entry in the measurements mapping, which every test iterates.
STRICT_PROBE: dict[bool, int] = {}
BODY_BYTES: dict[str, int] = {}


def _comparison(
    small: dict[bool, Measurement], large: dict[bool, Measurement], size: int
) -> str:
    def block(title: str, pair: dict[bool, Measurement]) -> list[str]:
        off_p50 = percentile(pair[False].deltas, 0.50)
        on_p50 = percentile(pair[True].deltas, 0.50)
        # Attributed from the medians, not the tails. A 15- or 30-sample tail on
        # Windows carries scheduling outliers big enough to make a p99 difference
        # change sign, which is a fact about the sample rather than about the
        # validator. Both are printed; the median is the one to read.
        cost = on_p50 - off_p50
        return [
            f"  -- {title}",
            f"     p50 added TTFT, strict off: {off_p50 * 1000:8.3f} ms",
            f"     p50 added TTFT, strict on : {on_p50 * 1000:8.3f} ms",
            f"     attributable to validation: {cost * 1000:8.3f} ms median "
            f"({cost / BUDGET_SECONDS * 100:.1f}% of the whole 100 ms budget)",
            f"     p99 added TTFT, off/on    : "
            f"{pair[False].p99_delta * 1000:.3f} / "
            f"{pair[True].p99_delta * 1000:.3f} ms",
        ]

    return "\n".join(
        [
            "",
            "=== strict_validation in the request path ===",
            *block("small body (1 message)", small),
            *block(f"large body ({size // 1024} KB, {LARGE_BODY_MESSAGES} messages)", large),
            "",
            "  02-04 measured the hoisted request validator in isolation at "
            "29.06 ms mean on a 155 KB / 161-message body and kept the default "
            "off against a 2 ms flip condition. The large-body row above is the "
            "same validator in the request path; read it, not the small-body "
            "row, when deciding the default.",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_the_injected_first_byte_delay_actually_took_effect(
    measurements: dict[str, Measurement],
) -> None:
    """Without this, the gate silently reverts to measuring localhost speed.

    A ``set_first_byte_delay`` that stopped working would leave a direct TTFT of
    around a millisecond, against which any relay overhead at all is a large
    percentage and the 100 ms branch of the budget is never approached. The gate
    would then pass unconditionally and prove nothing.
    """
    for measurement in measurements.values():
        floor = min(measurement.direct)
        assert floor >= FIRST_BYTE_DELAY_SECONDS * 0.95, (
            f"{measurement.label}: fastest direct TTFT was {floor * 1000:.1f} ms, "
            f"below the injected {FIRST_BYTE_DELAY_SECONDS * 1000:.0f} ms delay. "
            f"The delay injection is not in effect and this gate is vacuous."
        )
        assert min(measurement.gateway) >= FIRST_BYTE_DELAY_SECONDS * 0.95


def test_added_ttft_p99_is_within_the_release_gate_with_strict_validation_off(
    measurements: dict[str, Measurement],
) -> None:
    """design.md 15.4: added TTFT overhead under 1% or 100 ms p99.

    This is the shipping configuration: ``strict_validation`` defaults to off and
    no Phase 2 plan may change that.
    """
    measurement = measurements["off"]
    if measurement.contaminated:
        pytest.skip(
            f"machine load invalidated the sample: the gateway-free control's "
            f"own p99 sits {measurement.direct_spread * 1000:.1f} ms above its "
            f"median (ceiling {DIRECT_STABILITY_CEILING_SECONDS * 1000:.0f} ms), "
            f"so the delta cannot be attributed to the relay. This is a validity "
            f"precondition, not a relaxed threshold -- the gate below is "
            f"unchanged.\n" + measurement.report()
        )
    assert measurement.p99_delta <= measurement.allowance, (
        f"{measurement.label}: p99 added TTFT is "
        f"{measurement.p99_delta * 1000:.3f} ms, over the "
        f"{measurement.allowance * 1000:.1f} ms allowance.\n"
        + measurement.report()
    )


def test_added_ttft_p99_is_within_the_release_gate_with_strict_validation_on(
    measurements: dict[str, Measurement],
) -> None:
    """Reported whether or not it passes; the default is decided by measurement.

    If this fails while the previous test passes, the finding is that
    ``strict_validation`` cannot be turned on within the release budget -- which
    is a recommendation about the default, not a licence to edit ``src/``.
    """
    measurement = measurements["on"]
    if measurement.contaminated:
        pytest.skip(
            f"machine load invalidated the sample: the gateway-free control's "
            f"own p99 sits {measurement.direct_spread * 1000:.1f} ms above its "
            f"median (ceiling {DIRECT_STABILITY_CEILING_SECONDS * 1000:.0f} ms), "
            f"so the delta cannot be attributed to the relay. This is a validity "
            f"precondition, not a relaxed threshold -- the gate below is "
            f"unchanged.\n" + measurement.report()
        )
    assert measurement.p99_delta <= measurement.allowance, (
        f"{measurement.label}: p99 added TTFT is "
        f"{measurement.p99_delta * 1000:.3f} ms, over the "
        f"{measurement.allowance * 1000:.1f} ms allowance. strict_validation "
        f"cannot be enabled by default at this cost.\n" + measurement.report()
    )


def test_the_gate_is_expressed_on_p99_and_p95_is_only_context(
    measurements: dict[str, Measurement],
) -> None:
    """A guard against the gate quietly relaxing to the weaker statistic.

    p95 <= p99 always, so asserting p95 is strictly weaker on exactly the tail
    the budget bounds. This pins that the value being gated is the p99 one.
    """
    for measurement in measurements.values():
        assert measurement.p95_delta <= measurement.p99_delta
        assert measurement.p99_delta == max(measurement.deltas), (
            "below n=100 the nearest-rank p99 is the maximum observed delta, "
            "which is the strictest honest reading of the sample"
        )


def test_the_measurement_is_paired_and_complete(
    measurements: dict[str, Measurement],
) -> None:
    """The gated sample really is n>=30 paired observations, not a shorter run."""
    assert set(measurements) == {"off", "on", "large_off", "large_on"}
    for key in ("off", "on"):
        measurement = measurements[key]
        assert len(measurement.direct) == ITERATIONS >= 30
        assert len(measurement.gateway) == ITERATIONS
        assert len(measurement.deltas) == ITERATIONS
    for key in ("large_off", "large_on"):
        assert len(measurements[key].deltas) == LARGE_BODY_ITERATIONS


def test_the_strict_validation_default_was_not_changed_by_this_module() -> None:
    """This plan measures the flag; it does not flip it.

    02-04 measured hoisted request validation at 29.06 ms mean against a 2 ms
    flip condition and kept the default off. No Phase 2 plan may edit it,
    including this one, and the only way to be sure is to read it back.
    """
    from hermes_auto.config import DEFAULT_STRICT_VALIDATION, load_config

    assert DEFAULT_STRICT_VALIDATION is False
    assert load_config(None).gateway.strict_validation is False


def test_strict_validation_was_actually_live_in_the_measured_process(
    measurements: dict[str, Measurement],
) -> None:
    """Without this the two configurations could be the same one twice.

    ``strict_validation`` is written into a config file that a *detached
    subprocess* reads. Nothing else in this module would notice if the sidecar
    ignored it -- both runs would produce plausible numbers and the comparison
    would silently be noise. An off-schema request separates them: forwarded when
    the flag is off, rejected at the gateway when it is on.
    """
    assert STRICT_PROBE, "the fixture did not run"
    assert STRICT_PROBE[False] == 200, (
        f"with strict_validation off an off-schema request should be forwarded "
        f"and answered by the upstream, got {STRICT_PROBE[False]}"
    )
    assert STRICT_PROBE[True] == 400, (
        f"with strict_validation on an off-schema request should be rejected by "
        f"the gateway, got {STRICT_PROBE[True]}. The flag did not take effect, "
        f"so the two measured configurations are the same one twice."
    )


def test_the_validation_cost_is_measured_on_a_body_worth_measuring(
    measurements: dict[str, Measurement],
) -> None:
    """The recommendation about the default, stated as numbers.

    ``02-CONTEXT.md``'s flip condition was "if hoisted cost lands under 2 ms,
    flip the default to on". Plan 02-04 measured 29.06 ms mean in isolation on a
    155 KB / 161-message body and kept it off. This module measures the same
    validator in the request path, and the recommendation stands: the cost is far
    above the flip condition and buys nothing R5 needs, because with the flag off
    a malformed request is rejected by the *provider* -- which is exactly the
    error the caller would have seen talking to it directly.

    Asserted only in the direction that could invalidate the measurement: the
    body really is large, and enabling the flag really does cost measurable time.
    The precise figure is printed, not asserted, because it is a measurement.
    """
    assert BODY_BYTES["large"] > 100_000, BODY_BYTES
    off = percentile(measurements["large_off"].deltas, 0.50)
    on = percentile(measurements["large_on"].deltas, 0.50)
    cost = on - off
    print(
        f"\nRECOMMENDATION: keep gateway.strict_validation off. Measured median "
        f"added TTFT attributable to it, on a {BODY_BYTES['large'] // 1024} KB / "
        f"{LARGE_BODY_MESSAGES}-message body, is {cost * 1000:.2f} ms "
        f"({cost / BUDGET_SECONDS * 100:.1f}% of the 100 ms release budget) "
        f"against a 2 ms flip condition."
    )
    assert cost > 0.002, (
        f"validation cost measured at {cost * 1000:.2f} ms, at or under "
        f"02-CONTEXT's 2 ms flip condition. If that is reproducible the default "
        f"should be revisited -- by the plan that owns config.py, not by this "
        f"one."
    )


# ---------------------------------------------------------------------------
# The validity precondition, proved non-vacuous
# ---------------------------------------------------------------------------


def _synthetic(direct: list[float], gateway: list[float], health: list[float]) -> Measurement:
    return Measurement("synthetic", direct, gateway, health)


def test_the_contamination_guard_fires_only_on_machine_noise() -> None:
    """A guard that can never fire is a permanent "machine quiet" and a lie.

    Three constructed samples, because the guard has to do two things and not do
    a third: catch in-process jitter, catch a starved sidecar, and **never**
    excuse a genuinely slow relay. The last one is what would make it a
    weakening of the gate rather than a precondition on the measurement.
    """
    quiet_direct = [0.3010 + index * 0.00001 for index in range(30)]
    quiet_health = [0.0008 + index * 0.00001 for index in range(30)]
    quiet_gateway = [value + 0.004 for value in quiet_direct]

    clean = _synthetic(quiet_direct, quiet_gateway, quiet_health)
    assert not clean.contaminated, clean.contamination_detail()
    assert clean.p99_delta < BUDGET_SECONDS

    # 1. In-process jitter: the direct control's own tail blows out.
    jittery_direct = [*quiet_direct[:-1], 2.583]
    noisy = _synthetic(jittery_direct, quiet_gateway, quiet_health)
    assert noisy.contaminated
    assert noisy.direct_spread > DIRECT_STABILITY_CEILING_SECONDS

    # 2. A starved sidecar: the direct control stays flat and only the sidecar's
    #    do-nothing endpoint blows out. This is the case the direct control alone
    #    misses, and it is the one actually observed on this machine.
    starved_health = [*quiet_health[:-1], 0.280]
    starved_gateway = [*quiet_gateway[:-1], quiet_gateway[-1] + 0.280]
    starved = _synthetic(quiet_direct, starved_gateway, starved_health)
    assert not starved.direct_spread > DIRECT_STABILITY_CEILING_SECONDS, (
        "the direct control is flat here -- that is the point of the second one"
    )
    assert starved.contaminated
    assert starved.health_spread > HEALTH_STABILITY_CEILING_SECONDS

    # 3. A genuinely slow relay: both controls are flat, every relayed request is
    #    500 ms late. The guard must NOT fire, or it would excuse the exact
    #    regression the gate exists to catch.
    slow_relay = _synthetic(
        quiet_direct, [value + 0.500 for value in quiet_direct], quiet_health
    )
    assert not slow_relay.contaminated, (
        "the guard excused a uniformly slow relay -- it is a weakening of the "
        "gate, not a precondition on the measurement"
    )
    assert slow_relay.p99_delta > slow_relay.allowance
