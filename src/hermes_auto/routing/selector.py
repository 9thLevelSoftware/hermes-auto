"""Eligibility, complexity classification, and ordered candidate selection."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import secrets
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..config import CandidateConfig

TIERS: tuple[str, ...] = ("fast", "balanced", "strong")
_TIER_INDEX = {tier: index for index, tier in enumerate(TIERS)}
_IMAGE_TYPES = frozenset({"image", "image_url", "input_image"})


class RoutingError(Exception):
    """No configured candidate can safely handle the request."""


@dataclasses.dataclass
class RouteDecision:
    """A prompt-free, session-free explanation of one routing decision."""

    selected: CandidateConfig
    candidates: tuple[CandidateConfig, ...]
    detected_tier: str
    requested_tier: str
    score: int
    estimated_tokens: int
    filtered: dict[str, tuple[str, ...]]
    fingerprint: str
    pinned: str | None = None
    attempts: tuple[dict[str, str], ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "selected_candidate": self.selected.id,
            "selected_tier": self.selected.tier,
            "detected_tier": self.detected_tier,
            "requested_tier": self.requested_tier,
            "complexity_score": self.score,
            "estimated_input_tokens": self.estimated_tokens,
            "filters": [
                {"candidate": candidate_id, "reasons": list(reasons)}
                for candidate_id, reasons in self.filtered.items()
            ],
            "fallback_attempts": [dict(attempt) for attempt in self.attempts],
            "pinned": self.pinned,
        }


@dataclasses.dataclass
class _SessionRoute:
    candidate_id: str
    decision: RouteDecision


def _messages(body: Mapping[str, Any]) -> list[Any]:
    messages = body.get("messages")
    return messages if isinstance(messages, list) else []


def estimate_input_tokens(body: Mapping[str, Any]) -> int:
    """Conservatively estimate input tokens as serialized messages / four."""
    serialized = json.dumps(_messages(body), ensure_ascii=False, default=str)
    return math.ceil(len(serialized) / 4)


def _latest_user_message(body: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for message in reversed(_messages(body)):
        if isinstance(message, Mapping) and message.get("role") == "user":
            return message
    return None


def _text_length_and_fence(message: Mapping[str, Any] | None) -> tuple[int, bool]:
    if message is None:
        return 0, False
    content = message.get("content", "")
    if isinstance(content, str):
        return len(content), "```" in content
    serialized = json.dumps(content, ensure_ascii=False, default=str)
    return len(serialized), "```" in serialized


def _contains_image(value: Any) -> bool:
    if isinstance(value, Mapping):
        type_value = value.get("type")
        if isinstance(type_value, str) and type_value.lower() in _IMAGE_TYPES:
            return True
        if "image_url" in value or "input_image" in value:
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    return False


def has_image_input(body: Mapping[str, Any]) -> bool:
    return _contains_image(_messages(body))


def declares_tools(body: Mapping[str, Any]) -> bool:
    tools = body.get("tools")
    return isinstance(tools, list) and bool(tools)


def complexity_score(body: Mapping[str, Any]) -> int:
    score = 0
    if declares_tools(body):
        score += 1

    length, has_fence = _text_length_and_fence(_latest_user_message(body))
    if length >= 1_500 or has_fence:
        score += 1

    tokens = estimate_input_tokens(body)
    if tokens >= 8_000:
        score += 2
    elif tokens >= 2_000:
        score += 1

    if has_image_input(body):
        score += 2
    if len(_messages(body)) >= 20:
        score += 1
    return score


def detected_tier(score: int) -> str:
    if score <= 1:
        return "fast"
    if score <= 3:
        return "balanced"
    return "strong"


def compatibility_tier(virtual_model: str, classified_tier: str) -> str:
    mode = (virtual_model or "auto").strip().lower()
    if mode == "auto:quality":
        return "strong"
    if mode == "auto:economy":
        return "fast"
    return classified_tier


def eligible_candidates(
    candidates: Sequence[CandidateConfig],
    body: Mapping[str, Any],
) -> tuple[tuple[CandidateConfig, ...], dict[str, tuple[str, ...]]]:
    tools = declares_tools(body)
    vision = has_image_input(body)
    tokens = estimate_input_tokens(body)
    eligible: list[CandidateConfig] = []
    filtered: dict[str, tuple[str, ...]] = {}
    for candidate in candidates:
        reasons: list[str] = []
        if tools and not candidate.supports_tools:
            reasons.append("tools")
        if vision and not candidate.supports_vision:
            reasons.append("vision")
        if tokens > math.floor(candidate.context_window * 0.9):
            reasons.append("context")
        if reasons:
            filtered[candidate.id] = tuple(reasons)
        else:
            eligible.append(candidate)
    return tuple(eligible), filtered


def _tier_fallback_order(requested_tier: str) -> tuple[str, ...]:
    index = _TIER_INDEX.get(requested_tier, _TIER_INDEX["balanced"])
    stronger = TIERS[index + 1 :]
    weaker = tuple(reversed(TIERS[:index]))
    return (TIERS[index], *stronger, *weaker)


def ordered_candidates(
    candidates: Iterable[CandidateConfig], requested_tier: str
) -> tuple[CandidateConfig, ...]:
    configured = tuple(candidates)
    return tuple(
        candidate
        for tier in _tier_fallback_order(requested_tier)
        for candidate in configured
        if candidate.tier == tier
    )


def _fingerprint(body: Mapping[str, Any]) -> str:
    latest = _latest_user_message(body)
    # ASCII escaping keeps lone UTF-16 surrogates representable. The ingress
    # deliberately accepts them and re-emits them as JSON escapes, so routing
    # must not turn that valid passthrough case into a UnicodeEncodeError.
    payload = json.dumps(latest, ensure_ascii=True, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{len(_messages(body))}:{digest}"


def _latest_role(body: Mapping[str, Any]) -> str:
    messages = _messages(body)
    if not messages or not isinstance(messages[-1], Mapping):
        return ""
    role = messages[-1].get("role")
    return role if isinstance(role, str) else ""


class DecisionRouter:
    """Bounded, process-local turn and decision state."""

    def __init__(
        self,
        candidates: Sequence[CandidateConfig],
        *,
        max_sessions: int = 256,
        max_decisions: int = 256,
    ) -> None:
        if not candidates:
            raise RoutingError("no routing candidates are configured")
        self.candidates = tuple(candidates)
        self.max_sessions = max(1, max_sessions)
        self._sessions: OrderedDict[str, _SessionRoute] = OrderedDict()
        self._decisions: deque[RouteDecision] = deque(maxlen=max(1, max_decisions))
        self._session_salt = secrets.token_bytes(32)

    def _session_key(self, root_session_id: str) -> str:
        value = (root_session_id or "no-session").encode("utf-8", "replace")
        return hashlib.sha256(self._session_salt + value).hexdigest()

    def _remember(self, key: str, decision: RouteDecision) -> None:
        self._sessions[key] = _SessionRoute(decision.selected.id, decision)
        self._sessions.move_to_end(key)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
        self._decisions.append(decision)

    def route(
        self,
        body: Mapping[str, Any],
        *,
        root_session_id: str,
        virtual_model: str,
    ) -> RouteDecision:
        eligible, filtered = eligible_candidates(self.candidates, body)
        if not eligible:
            detail = ", ".join(
                f"{candidate_id} ({'/'.join(reasons)})"
                for candidate_id, reasons in filtered.items()
            )
            raise RoutingError(f"no eligible candidates: {detail or 'none configured'}")

        score = complexity_score(body)
        classified = detected_tier(score)
        requested = compatibility_tier(virtual_model, classified)
        ordered = ordered_candidates(eligible, requested)
        key = self._session_key(root_session_id)
        previous = self._sessions.get(key)
        pinned: str | None = None

        should_pin_tool_loop = _latest_role(body) != "user"
        should_pin_session = (virtual_model or "").strip().lower() == "auto:session"
        if previous is not None and (should_pin_tool_loop or should_pin_session):
            pinned_candidate = next(
                (item for item in eligible if item.id == previous.candidate_id),
                None,
            )
            if pinned_candidate is not None:
                ordered = (
                    pinned_candidate,
                    *(item for item in ordered if item.id != pinned_candidate.id),
                )
                pinned = "tool_loop" if should_pin_tool_loop else "session"

        decision = RouteDecision(
            selected=ordered[0],
            candidates=ordered,
            detected_tier=classified,
            requested_tier=requested,
            score=score,
            estimated_tokens=estimate_input_tokens(body),
            filtered=filtered,
            fingerprint=_fingerprint(body),
            pinned=pinned,
        )
        self._remember(key, decision)
        return decision

    def finalize(
        self,
        decision: RouteDecision,
        selected: CandidateConfig,
        attempts: Sequence[Mapping[str, str]],
    ) -> None:
        decision.selected = selected
        decision.attempts = tuple(dict(item) for item in attempts)
        for state in self._sessions.values():
            if state.decision is decision:
                state.candidate_id = selected.id
                break

    def decision_for(self, root_session_id: str) -> RouteDecision | None:
        state = self._sessions.get(self._session_key(root_session_id))
        return state.decision if state is not None else None

    def latest_decisions(self) -> tuple[RouteDecision, ...]:
        return tuple(self._decisions)

    def latest_decision(self) -> RouteDecision | None:
        return self._decisions[-1] if self._decisions else None
