"""Declarative multi-stage background pipeline (langgraph transition model).

Public API::

    from mote.orchestration.tasks.bggraph import BgGraph, GraphState, Stage, START, END, BaseNode
"""

from __future__ import annotations

from mote.orchestration.tasks.bggraph.base_node import BaseNode, From
from mote.orchestration.tasks.bggraph.channels import Output
from mote.orchestration.tasks.bggraph.graph import BgGraph
from mote.orchestration.tasks.bggraph.types import (
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
