"""Immutable Product view reconstructed from canonical durable Workflow facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from mote.contracts.events.envelope import JsonValue
from mote.contracts.workflow import WorkflowRunReference
from mote.orchestration.workflows.durable import WorkflowRunPhase
from mote.orchestration.workflows.types import GraphRunState, NodeParameterSpec


@dataclass(frozen=True, slots=True)
class WorkflowNodeView:
    name: str
    description: str
    params: Mapping[str, NodeParameterSpec]


@dataclass(frozen=True, slots=True)
class WorkflowGraphView:
    nodes: Mapping[str, WorkflowNodeView]
    self_loop_nodes: frozenset[str]

    def is_self_loop(self, name: str) -> bool:
        return name in self.self_loop_nodes


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    reference: WorkflowRunReference
    run_state: GraphRunState
    state_snapshot: Mapping[str, JsonValue]
    graph: WorkflowGraphView
    status: WorkflowRunPhase = WorkflowRunPhase.CREATED

    @property
    def command_name(self) -> str:
        return str(self.reference.definition_id)

    @property
    def completed_nodes(self) -> frozenset[str]:
        return frozenset(self.run_state.completed_names())


__all__ = ["WorkflowGraphView", "WorkflowNodeView", "WorkflowRunView"]
