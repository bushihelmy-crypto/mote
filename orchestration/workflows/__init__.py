"""Declarative multi-stage background pipeline (langgraph transition model).

Public API::

    from mote.orchestration.workflows import WorkflowBuilder, GraphState, Stage, START, END, BaseNode
"""

from __future__ import annotations

from mote.contracts.task.graph_errors import (
    GraphBatchFailureError,
    GraphParamTypeError,
    GraphRecursionError,
    GraphRouterError,
)
from mote.orchestration.workflows.base_node import BaseNode, From
from mote.orchestration.workflows.channels import NoOutput, Output
from mote.orchestration.workflows.control import PauseReason
from mote.orchestration.workflows.definition import (
    Cancelled,
    Failed,
    Paused,
    RunSnapshot,
    Succeeded,
    TimedOut,
    WorkflowContinuation,
    WorkflowDefinition,
    WorkflowOutcome,
    WorkflowRun,
)
from mote.orchestration.workflows.graph import WorkflowBuilder
from mote.orchestration.workflows.types import (
    END,
    START,
    BgStatus,
    GraphPause,
    GraphRunState,
    GraphState,
    NodeRecord,
    Stage,
)

__all__ = [
    "BaseNode",
    "From",
    "WorkflowBuilder",
    "WorkflowDefinition",
    "WorkflowContinuation",
    "WorkflowRun",
    "WorkflowOutcome",
    "Succeeded",
    "Failed",
    "Paused",
    "RunSnapshot",
    "Cancelled",
    "TimedOut",
    "Output",
    "NoOutput",
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
