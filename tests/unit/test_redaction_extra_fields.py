"""``logger.info(msg, extra={...})`` must not bypass the redaction sink.

``RedactingFormatter`` handled ``record.msg`` and ``record.args`` and nothing
else. ``extra=`` is neither: ``Logger.makeRecord`` copies each key straight onto
the record as an attribute, so ``logger.info("x", extra={"api_key": secret})``
reached the output untouched by a sink whose entire stated purpose is that
"nothing in this project logs an event dict directly".

**This gap is prospective, and that is stated rather than implied.** No call site
in ``src/`` passes ``extra=`` today -- verified by search -- and the default
format string renders only ``levelname``, ``name`` and ``message``, so surfacing
an extra attribute additionally requires a format string that names it. So the
sink was never actually leaking. It is closed because "no current call site does
this" is a property of today's code that no test enforced, and the sink is
supposed to be a boundary rather than a convention -- a boundary with a hole in
it that everyone politely avoids is a convention.

**Banned keys are replaced, not removed.** ``redact`` drops banned keys from a
mapping, but an *attribute* that a format string names cannot be dropped: the
formatter would raise ``KeyError`` and logging would swallow it, converting a
redaction into a silently missing log line. So a banned extra keeps its
attribute and loses its value.
"""

from __future__ import annotations

import logging

import pytest

from hermes_auto.telemetry.redaction import (
    EXTRA_REDACTED,
    RedactingFormatter,
    session_digest,
)

RAW_SESSION_ID = "hermes-session-9f2c4a11-not-a-hash"


def _record(fmt: str, **extra: object) -> logging.LogRecord:
    record = logging.makeLogRecord(
        {
            "name": "hermes_auto.test.extra",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": {"event": "gateway.started"},
        }
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


@pytest.mark.parametrize("banned", ["api_key", "token", "authorization", "secret"])
def test_a_banned_key_passed_as_extra_is_redacted(banned: str) -> None:
    formatter = RedactingFormatter(fmt=f"%(message)s %({banned})s")
    output = formatter.format(_record(banned, **{banned: "SECRET-VALUE"}))

    assert "SECRET-VALUE" not in output
    assert EXTRA_REDACTED in output


def test_a_banned_extra_keeps_its_attribute_so_the_format_cannot_raise() -> None:
    """Dropping the attribute would turn a redaction into a missing log line.

    ``logging`` catches the formatter's exception and writes a traceback to
    stderr instead of the record, so the operator loses the event *and* gets no
    obvious signal that redaction is why.
    """
    formatter = RedactingFormatter(fmt="%(message)s key=%(api_key)s")
    output = formatter.format(_record("api_key", api_key="SECRET-VALUE"))
    assert "key=" in output


def test_a_nested_secret_inside_an_extra_value_is_redacted() -> None:
    """The recursion that ``redact`` already does must reach extras too."""
    formatter = RedactingFormatter(fmt="%(message)s %(payload)s")
    output = formatter.format(
        _record("payload", payload={"outer": {"api_key": "SECRET-VALUE"}})
    )
    assert "SECRET-VALUE" not in output


def test_a_session_id_in_an_extra_is_hashed_not_dropped() -> None:
    """Session ids get the same salted-digest treatment as in the payload."""
    formatter = RedactingFormatter(fmt="%(message)s %(payload)s")
    output = formatter.format(
        _record("payload", payload={"session_id": RAW_SESSION_ID})
    )
    assert RAW_SESSION_ID not in output
    assert session_digest(RAW_SESSION_ID) in output


def test_a_benign_extra_survives_unchanged() -> None:
    """Redaction is not an excuse to mangle ordinary diagnostic fields."""
    formatter = RedactingFormatter(fmt="%(message)s %(request_id)s")
    output = formatter.format(_record("request_id", request_id="req-123"))
    assert "req-123" in output


def test_standard_record_fields_are_left_alone() -> None:
    """``name``, ``levelname`` and friends are not ``extra`` and must not move."""
    formatter = RedactingFormatter(fmt="%(name)s %(levelname)s %(message)s")
    output = formatter.format(_record("unused"))
    assert output.startswith("hermes_auto.test.extra INFO ")
