from __future__ import annotations

import pytest

from hermes_auto.config import CandidateConfig
from hermes_auto.routing import (
    DecisionRouter,
    compatibility_tier,
    complexity_score,
    eligible_candidates,
    estimate_input_tokens,
    ordered_candidates,
)


def candidate(
    candidate_id: str,
    tier: str,
    *,
    context_window: int = 128_000,
    supports_tools: bool = True,
    supports_vision: bool = True,
) -> CandidateConfig:
    return CandidateConfig(
        id=candidate_id,
        provider="test",
        model=f"real-{candidate_id}",
        base_url="https://provider.example/v1",
        credential_ref="none",
        tier=tier,
        context_window=context_window,
        supports_tools=supports_tools,
        supports_vision=supports_vision,
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"messages": [{"role": "user", "content": "a" * 4}]}, 10),
        (
            {
                "messages": [
                    {"role": "system", "content": "1234"},
                    {"role": "user", "content": "5678"},
                ]
            },
            19,
        ),
    ],
)
def test_token_estimate_is_ceiling_of_serialized_message_characters(
    body: dict[str, object], expected: int
) -> None:
    assert estimate_input_tokens(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"messages": [{"role": "user", "content": "short"}]}, 0),
        (
            {"messages": [{"role": "user", "content": "```python\nprint(1)\n```"}]},
            1,
        ),
        ({"messages": [{"role": "user", "content": "x" * 1_500}]}, 1),
        (
            {"messages": [{"role": "user", "content": "x" * 7_900}]},
            1,
        ),
        (
            {"messages": [{"role": "user", "content": "x" * 8_000}]},
            2,
        ),
        (
            {"messages": [{"role": "user", "content": "x" * 32_000}]},
            3,
        ),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                    }
                ]
            },
            2,
        ),
        (
            {
                "messages": [{"role": "user", "content": "short"}] * 20,
                "tools": [{"type": "function", "function": {"name": "x"}}],
            },
            2,
        ),
    ],
)
def test_complexity_score_thresholds(body: dict[str, object], expected: int) -> None:
    assert complexity_score(body) == expected


def test_eligibility_reports_each_capability_and_context_filter() -> None:
    candidates = (
        candidate("no-tools", "fast", context_window=100, supports_tools=False),
        candidate("no-vision", "balanced", context_window=100, supports_vision=False),
        candidate("too-small", "strong", context_window=100),
        candidate("eligible", "strong"),
    )
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x" * 1_000},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                ],
            }
        ],
        "tools": [{"type": "function", "function": {"name": "x"}}],
    }

    eligible, filtered = eligible_candidates(candidates, body)

    assert [item.id for item in eligible] == ["eligible"]
    assert filtered == {
        "no-tools": ("tools", "context"),
        "no-vision": ("vision", "context"),
        "too-small": ("context",),
    }


@pytest.mark.parametrize(
    ("virtual_model", "detected_tier", "expected"),
    [
        ("auto", "balanced", "balanced"),
        ("auto:balanced", "fast", "fast"),
        ("auto:quality", "fast", "strong"),
        ("auto:economy", "strong", "fast"),
        ("auto:session", "balanced", "balanced"),
    ],
)
def test_compatibility_modes(
    virtual_model: str, detected_tier: str, expected: str
) -> None:
    assert compatibility_tier(virtual_model, detected_tier) == expected


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("fast", ["fast-a", "fast-b", "balanced", "strong"]),
        ("balanced", ["balanced", "strong", "fast-a", "fast-b"]),
        ("strong", ["strong", "balanced", "fast-a", "fast-b"]),
    ],
)
def test_candidate_order_uses_config_order_with_stronger_then_weaker_fallback(
    requested: str, expected: list[str]
) -> None:
    candidates = (
        candidate("fast-a", "fast"),
        candidate("strong", "strong"),
        candidate("fast-b", "fast"),
        candidate("balanced", "balanced"),
    )
    assert [item.id for item in ordered_candidates(candidates, requested)] == expected


def test_new_user_turn_reselects_while_tool_results_keep_the_turn_choice() -> None:
    router = DecisionRouter(
        (
            candidate("fast", "fast"),
            candidate("strong", "strong"),
        )
    )
    first = router.route(
        {
            "messages": [{"role": "user", "content": "short"}],
        },
        root_session_id="session-a",
        virtual_model="auto",
    )
    tool_loop = router.route(
        {
            "messages": [
                {"role": "user", "content": "short"},
                {"role": "assistant", "tool_calls": [{"id": "1"}]},
                {"role": "tool", "tool_call_id": "1", "content": "result"},
            ],
        },
        root_session_id="session-a",
        virtual_model="auto",
    )
    next_turn = router.route(
        {
            "messages": [
                {"role": "user", "content": "short"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "x" * 32_000},
            ],
        },
        root_session_id="session-a",
        virtual_model="auto",
    )

    assert first.selected.id == "fast"
    assert tool_loop.selected.id == "fast"
    assert tool_loop.pinned == "tool_loop"
    assert next_turn.selected.id == "strong"
    assert next_turn.pinned is None


def test_session_mode_remains_pinned_across_user_turns_and_sessions_are_isolated() -> None:
    router = DecisionRouter(
        (
            candidate("fast", "fast"),
            candidate("strong", "strong"),
        )
    )
    first = router.route(
        {"messages": [{"role": "user", "content": "short"}]},
        root_session_id="session-a",
        virtual_model="auto:session",
    )
    pinned = router.route(
        {"messages": [{"role": "user", "content": "x" * 32_000}]},
        root_session_id="session-a",
        virtual_model="auto:session",
    )
    other = router.route(
        {"messages": [{"role": "user", "content": "x" * 32_000}]},
        root_session_id="session-b",
        virtual_model="auto:session",
    )

    assert first.selected.id == "fast"
    assert pinned.selected.id == "fast"
    assert pinned.pinned == "session"
    assert other.selected.id == "strong"


def test_repeated_user_text_with_a_new_message_count_is_a_new_turn() -> None:
    router = DecisionRouter((candidate("fast", "fast"), candidate("strong", "strong")))
    first = router.route(
        {"messages": [{"role": "user", "content": "repeat"}]},
        root_session_id="session-a",
        virtual_model="auto",
    )
    second = router.route(
        {
            "messages": [
                {"role": "user", "content": "repeat"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "repeat"},
            ]
        },
        root_session_id="session-a",
        virtual_model="auto",
    )
    assert first.fingerprint != second.fingerprint
    assert second.pinned is None


def test_decision_state_is_bounded_and_does_not_expose_session_ids() -> None:
    router = DecisionRouter((candidate("fast", "fast"),), max_sessions=2, max_decisions=2)
    for session_id in ("secret-a", "secret-b", "secret-c"):
        router.route(
            {"messages": [{"role": "user", "content": session_id}]},
            root_session_id=session_id,
            virtual_model="auto",
        )

    assert router.decision_for("secret-a") is None
    assert router.decision_for("secret-c") is not None
    serialized = repr(router.latest_decisions())
    assert "secret-a" not in serialized
    assert "secret-b" not in serialized
    assert "secret-c" not in serialized
