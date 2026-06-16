"""Declarative multi-stage background pipeline (langgraph transition model).

Public API::

    from metagpt.executor.tasks.bggraph import BgGraph, GraphState, Stage, START, END
"""

from __future__ import annotations

from metagpt.executor.tasks.bggraph.graph import BgGraph
from metagpt.executor.tasks.bggraph.types import (
    END,
    START,
    GraphBatchFailureError,
    GraphRecursionError,
    GraphRouterError,
    GraphState,
    LlmPauseResult,
    BgStatus,
    Stage,
)

__all__ = [
    "BgGraph",
    "GraphState",
    "Stage",
    "BgStatus",
    "LlmPauseResult",
    "START",
    "END",
    "GraphRouterError",
    "GraphRecursionError",
    "GraphBatchFailureError",
]
