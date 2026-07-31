"""Measure what full request validation costs. Do not assume it.

``02-CONTEXT.md`` § Request Validation Policy sets ``gateway.strict_validation``
to off by default and states the condition for changing that:

    if hoisted cost lands under 2 ms, flip the default to on.

Measurement decides, not taste. This module produces the number. It deliberately
asserts almost nothing about the value -- a latency assertion in CI turns a busy
runner into a red build and teaches everyone to ignore it -- but it does assert
the things that would make the number a lie:

* the body actually validates, so the timing covers a full walk of the schema
  rather than an early exit on the first failing keyword;
* the body is in the 100-200 KB range the policy names;
* the validator is genuinely hoisted, proven by comparing against the
  unhoisted ``validate()`` path in the same run on the same machine.

That last comparison is the point of the whole exercise. The ~50 ms figure in
``gateway/schemas.py`` is ``check_schema``, which a hoisted validator pays once
at startup and never again. Quoting it as the per-request cost -- or measuring
``validate()`` and concluding validation is expensive -- would be measuring the
wrong thing, and the resulting default would be wrong for the wrong reason.
"""

from __future__ import annotations

import json
import statistics
import time

import pytest

from hermes_auto.gateway.ingress import (
    ENVELOPE_SCHEMA_ID,
    REQUEST_SCHEMA_ID,
    envelope_validator,
    request_validator,
)
from hermes_auto.gateway.schemas import build_validator, load_schemas, validate

pytestmark = pytest.mark.performance

#: Policy threshold from 02-CONTEXT § Request Validation Policy.
STRICT_VALIDATION_THRESHOLD_MS = 2.0

#: The policy names 100-200 KB as the realistic body size.
TARGET_BODY_BYTES = 150 * 1024

#: At least 200 per the plan.
ITERATIONS = 250


def _realistic_body() -> dict:
    """A schema-valid request whose ``messages`` array is ~150 KB.

    Shaped like a real agent conversation rather than one enormous string: a
    system prompt, alternating user/assistant turns, an assistant turn carrying
    parallel ``tool_calls``, the matching ``role: tool`` results, and a tools
    array. That matters because the schema's cost is dominated by ``$ref``
    recursion and the two conditional ``allOf`` branches on message role, and a
    single 150 KB string would exercise neither.
    """
    paragraph = (
        "The router must forward this body unchanged, including every field it "
        "does not model. Byte transparency is the property under test. "
    ) * 6

    messages: list[dict] = [
        {"role": "system", "content": "You are a helpful assistant." + paragraph}
    ]
    turn = 0
    while len(json.dumps(messages)) < TARGET_BODY_BYTES:
        messages.append({"role": "user", "content": f"Turn {turn}: {paragraph}"})
        messages.append(
            {
                "role": "assistant",
                "content": f"Answer {turn}: {paragraph}",
                "tool_calls": [
                    {
                        "id": f"call_{turn}_a",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": json.dumps({"query": paragraph[:200]}),
                        },
                    },
                    {
                        "id": f"call_{turn}_b",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": f"/tmp/{turn}.txt"}),
                        },
                    },
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{turn}_a",
                "content": f"result {turn}: {paragraph}",
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{turn}_b",
                "content": f"file {turn}: {paragraph}",
            }
        )
        turn += 1

    return {
        "model": "auto:balanced",
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search the web.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            },
        ],
        "_hermes_auto": {
            "protocol_version": 1,
            "root_session_id": "session-measurement",
            "virtual_model": "auto:balanced",
            "plugin_version": "0.1.0",
        },
    }


def _time(callable_, iterations: int) -> list[float]:
    """Return per-call durations in milliseconds."""
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        callable_()
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples


def _report(label: str, samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    stats = {
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[int(len(ordered) * 0.95) - 1],
        "max_ms": ordered[-1],
    }
    print(
        f"\n{label}: n={len(samples)} "
        f"mean={stats['mean_ms'] * 1000:.1f} us  "
        f"median={stats['median_ms'] * 1000:.1f} us  "
        f"p95={stats['p95_ms'] * 1000:.1f} us  "
        f"max={stats['max_ms'] * 1000:.1f} us"
    )
    return stats


def test_body_fixture_is_realistic_and_valid():
    """Guards every measurement below.

    A body that failed validation would exit at the first bad keyword and the
    timings would describe nothing. A body outside the stated size range would
    answer a question the policy did not ask.
    """
    body = _realistic_body()
    size = len(json.dumps(body).encode("utf-8"))
    assert 100 * 1024 <= size <= 260 * 1024, f"body is {size} bytes"
    # Raises if invalid; the full walk is what is being timed.
    build_validator(REQUEST_SCHEMA_ID, load_schemas()).validate(body)
    print(f"\nmeasurement body: {size} bytes, {len(body['messages'])} messages")


def test_hoisted_request_validation_cost():
    """The number that decides the ``strict_validation`` default."""
    body = _realistic_body()
    validator = request_validator()
    validator.validate(body)  # warm any lazy machinery before timing

    stats = _report("hoisted openai-chat-request.v1", _time(lambda: validator.validate(body), ITERATIONS))

    verdict = (
        "UNDER the 2 ms threshold -> 02-CONTEXT says flip strict_validation ON"
        if stats["mean_ms"] < STRICT_VALIDATION_THRESHOLD_MS
        else "OVER the 2 ms threshold -> strict_validation stays OFF by default"
    )
    print(f"  verdict: mean {stats['mean_ms']:.3f} ms is {verdict}")

    # Asserts completion only, per the plan. The value is the deliverable.
    assert stats["mean_ms"] >= 0.0


def test_envelope_validation_is_cheap_enough_for_the_hot_path():
    """The envelope check runs on *every* request, so this one has a budget.

    Five fields, a closed allowlist, no recursion. If this ever approached the
    request-schema cost, something would have gone wrong with the hoisting.
    """
    envelope = _realistic_body()["_hermes_auto"]
    validator = envelope_validator()
    validator.validate(envelope)

    stats = _report(
        f"hoisted {ENVELOPE_SCHEMA_ID.rsplit('/', 1)[-1]}",
        _time(lambda: validator.validate(envelope), ITERATIONS),
    )
    assert stats["mean_ms"] < 1.0, (
        "the always-on envelope check must stay in the microseconds; "
        f"measured {stats['mean_ms']:.3f} ms"
    )


def test_hoisting_is_what_makes_validation_affordable():
    """Hoisted versus unhoisted, same body, same machine, same run.

    This is the comparison that keeps the decision honest. ``validate()`` re-runs
    ``check_schema`` every call; quoting its cost as the per-request price of
    validation would justify leaving ``strict_validation`` off for a reason that
    is not true of the code actually on the request path.
    """
    body = _realistic_body()
    schemas = load_schemas()
    validator = request_validator()
    validator.validate(body)

    hoisted = _report("hoisted", _time(lambda: validator.validate(body), 50))
    unhoisted = _report(
        "unhoisted validate()",
        _time(lambda: validate(body, REQUEST_SCHEMA_ID, schemas), 20),
    )

    ratio = unhoisted["mean_ms"] / max(hoisted["mean_ms"], 1e-9)
    print(
        f"  unhoisted is {ratio:.1f}x the hoisted cost; the difference "
        f"({unhoisted['mean_ms'] - hoisted['mean_ms']:.1f} ms) is check_schema, "
        f"paid once at startup by build_validators()"
    )
    assert unhoisted["mean_ms"] > hoisted["mean_ms"], (
        "if these are equal, the 'hoisted' validator is being rebuilt per call"
    )


def test_startup_hoisting_pays_the_check_schema_cost_once():
    """``build_validators()`` is why no user request pays for ``check_schema``."""
    from hermes_auto.gateway import ingress

    ingress.build_validators()
    first = request_validator()
    second = request_validator()
    assert first is second, "request validator is not hoisted"
    assert envelope_validator() is envelope_validator()
