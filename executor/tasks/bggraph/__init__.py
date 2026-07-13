"""Declarative multi-stage background pipeline (langgraph transition model).

Public API::

    from mote.executor.tasks.bggraph import BgGraph, GraphState, Stage, START, END, BaseNode
"""

from __future__ import annotations

from mote.executor.tasks.bggraph.base_node import BaseNode, From
from mote.executor.tasks.bggraph.channels import Output
from mote.executor.tasks.bggraph.graph import BgGraph
from mote.executor.tasks.bggraph.types import (
    END,
    START,
    BgStatus,
    GraphBatchFailureError,
    GraphParamTypeError,
    GraphPause,
    GraphRecursionError,
    GraphRouterError,
    GraphRunState,
    GraphState,
    NodeRecord,
    PauseReason,
    Stage,
)

__all__ = [
    "BaseNode",
    "From",
    "BgGraph",
    "Output",
    "GraphState",
    "GraphRunState",
    "NodeRecord",
    "Stage",
    "BgStatus",
    "GraphPause",
    "PauseReason",
    "START",
    "END",
    "GraphRouterError",
    "GraphRecursionError",
    "GraphBatchFailureError",
    "GraphParamTypeError",
]
