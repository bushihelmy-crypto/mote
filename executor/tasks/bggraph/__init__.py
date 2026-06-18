"""Declarative multi-stage background pipeline (langgraph transition model).

Public API::

    from metagpt.executor.tasks.bggraph import BgGraph, GraphState, Stage, START, END, BaseNode
"""

from __future__ import annotations

from metagpt.executor.tasks.bggraph.base_node import BaseNode, From
from metagpt.executor.tasks.bggraph.graph import BgGraph
from metagpt.executor.tasks.bggraph.types import (
    END,
    START,
    GraphBatchFailureError,
    GraphParamTypeError,
    GraphRecursionError,
    GraphRouterError,
    GraphRunState,
    GraphState,
    LlmPauseResult,
    NodeRecord,
    BgStatus,
    Stage,
)

__all__ = [
    "BaseNode",
    "From",
    "BgGraph",
    "GraphState",
    "GraphRunState",
    "NodeRecord",
    "Stage",
    "BgStatus",
    "LlmPauseResult",
    "START",
    "END",
    "GraphRouterError",
    "GraphRecursionError",
    "GraphBatchFailureError",
    "GraphParamTypeError",
]
