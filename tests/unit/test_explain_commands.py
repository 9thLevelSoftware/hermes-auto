from __future__ import annotations

import io

from hermes_auto import commands
from hermes_auto.cli import build_parser


DECISION = {
    "selected_candidate": "strong",
    "selected_tier": "strong",
    "detected_tier": "balanced",
    "requested_tier": "balanced",
    "complexity_score": 3,
    "estimated_input_tokens": 8100,
    "filters": [{"candidate": "fast", "reasons": ["context"]}],
    "fallback_attempts": [{"candidate": "balanced", "reason": "http_429"}],
    "pinned": "tool_loop",
}


def test_explain_command_formats_the_decision_without_internal_state(monkeypatch) -> None:
    monkeypatch.setattr(commands, "_admin_json", lambda path, config=None: (200, DECISION))
    output = io.StringIO()

    code = commands.cmd_explain(session_id="latest", stream=output)

    assert code == 0
    text = output.getvalue()
    assert "selected: strong (strong)" in text
    assert "detected complexity: balanced (score 3)" in text
    assert "filtered fast: context" in text
    assert "fallback balanced: http_429" in text
    assert "pinned: tool_loop" in text


def test_overview_includes_health_candidate_count_and_latest_decision(
    monkeypatch,
) -> None:
    class Status:
        detail = "gateway is running"

    monkeypatch.setattr(commands.supervisor, "status", lambda config=None: Status())
    monkeypatch.setattr(
        commands,
        "_admin_json",
        lambda path, config=None: (
            200,
            {
                "configured_candidate_count": 2,
                "most_recent_decision": DECISION,
            },
        ),
    )
    output = io.StringIO()

    code = commands.cmd_overview(stream=output)

    assert code == 0
    text = output.getvalue()
    assert "gateway is running" in text
    assert "configured candidates: 2" in text
    assert "most recent: strong (strong)" in text


def test_cli_exposes_configure_and_explain() -> None:
    parser = build_parser()
    assert parser.parse_args(["configure"]).command == "configure"
    explain = parser.parse_args(["explain", "--session-id", "abc"])
    assert explain.command == "explain"
    assert explain.session_id == "abc"
