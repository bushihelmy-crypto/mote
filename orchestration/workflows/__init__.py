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
from mote.orchestration.workflows.channels import NoOutput, Output, Reducer
from mote.orchestration.workflows.control import PauseReason
from mote.orchestration.workflows.definition import (
    Cancelled,
    Failed,
    Paused,
    RunSnapshot,
    Succeeded,
    TimedOut,
    WorkflowDefinition,
    WorkflowDefinitionCompiler,
    WorkflowExecutable,
    WorkflowOutcome,
    WorkflowRun,
)
from mote.orchestration.workflows.graph import WorkflowBuilder
from mote.orchestration.workflows.types import (
    END,
    START,
    GraphPause,
    GraphRunState,
    GraphState,
    NodeRecord,
    Stage,
    WorkflowNodeStatus,
)

__all__ = [
    "BaseNode",
    "From",
    "WorkflowBuilder",
    "WorkflowDefinition",
    "WorkflowDefinitionCompiler",
    "WorkflowExecutable",
    "WorkflowRun",
    "WorkflowOutcome",
    "Succeeded",
    "Failed",
    "Paused",
    "RunSnapshot",
    "Cancelled",
    "TimedOut",
    "Output",
    "Reducer",
    "NoOutput",
    "GraphState",
    "GraphRunState",
    "NodeRecord",
    "Stage",
    "WorkflowNodeStatus",
    "GraphPause",
    "PauseReason",
    "START",
    "END",
    "GraphRouterError",
    "GraphRecursionError",
    "GraphBatchFailureError",
    "GraphParamTypeError",
]
