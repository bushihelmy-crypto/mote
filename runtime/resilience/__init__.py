"""Domain-agnostic resilience primitives.

Currently the sliding-window :class:`CircuitBreaker` and its :class:`BreakerConfig`.
Woven into LLM provider selection today; reusable for any failing-resource guard
(MCP, egress, spawn) tomorrow. See :mod:`mote.runtime.resilience.breaker`.
"""

from __future__ import annotations

from mote.contracts.resilience import BreakerConfig

from .breaker import MAX_WINDOW_ENTRIES, BreakerState, CircuitBreaker, TransitionHook
from .registry import ResourceHealthRegistry

__all__ = [
    "BreakerConfig",
    "CircuitBreaker",
    "BreakerState",
    "TransitionHook",
    "MAX_WINDOW_ENTRIES",
    "ResourceHealthRegistry",
]
