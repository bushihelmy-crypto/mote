"""Domain-agnostic resilience primitives.

Currently the sliding-window :class:`CircuitBreaker` and its :class:`BreakerConfig`.
Woven into LLM provider selection today; reusable for any failing-resource guard
(MCP, egress, spawn) tomorrow. See :mod:`mote.common.resilience.breaker`.
"""

from __future__ import annotations

from .breaker import MAX_WINDOW_ENTRIES, BreakerState, CircuitBreaker, TransitionHook
from .config import BreakerConfig
from .registry import ResourceHealthRegistry, configure_health_registry, get_health_registry, reset_health_registry

__all__ = [
    "BreakerConfig",
    "CircuitBreaker",
    "BreakerState",
    "TransitionHook",
    "MAX_WINDOW_ENTRIES",
    "ResourceHealthRegistry",
    "get_health_registry",
    "reset_health_registry",
    "configure_health_registry",
]
