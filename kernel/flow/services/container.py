"""Explicit service bundle consumed by built-in graph topology."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mote.kernel.flow.context import FlowContext


@dataclass(frozen=True)
class FlowServices:
    """All behavior a graph may coordinate, with no Role or Engine reference."""

    context: Callable[[], FlowContext]
    observation: Any
    think: Any
    actions: Any
    outputs: Any
    context_provider: Any
    completion_policy: Any
    current_channel: Callable[[], Any]
    think_engine: Any
    set_active: Callable[[bool], None]
    get_bg_pool: Callable[[], Any]
    advance_turn: Callable[[], int] | None


__all__ = ["FlowServices"]
