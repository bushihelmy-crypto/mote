"""Declarative multi-stage background pipeline (langgraph transition model).

Public API::

    from metagpt.tasks.bggraph import BgGraph, GraphState, Stage, START, END
"""

from __future__ import annotations

from metagpt.tasks.bggraph.graph import BgGraph
from metagpt.tasks.bggraph.types import (
    END,
    START,
    BatchFailureError,
    GraphRecursionError,
    GraphState,
    NodeStatus,
    RouterError,
    Stage,
)

__all__ = [
    "BgGraph",
    "GraphState",
    "Stage",
    "NodeStatus",
    "START",
    "END",
    "RouterError",
    "GraphRecursionError",
    "BatchFailureError",
]
