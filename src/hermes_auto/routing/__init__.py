"""Deterministic, in-memory model selection for ``/model auto``."""

from .selector import (
    DecisionRouter,
    RouteDecision,
    RoutingError,
    compatibility_tier,
    complexity_score,
    eligible_candidates,
    estimate_input_tokens,
    ordered_candidates,
)

__all__ = [
    "DecisionRouter",
    "RouteDecision",
    "RoutingError",
    "compatibility_tier",
    "complexity_score",
    "eligible_candidates",
    "estimate_input_tokens",
    "ordered_candidates",
]
