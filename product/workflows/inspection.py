"""Immutable Product view reconstructed from canonical durable Workflow facts."""

from __future__ import annotations

from dataclasses import dataclass, field

from mote.contracts.workflow import WorkflowRunReference
from mote.orchestration.workflows import WorkflowRun
from mote.orchestration.workflows.deferred import WorkflowRunMetadata
from mote.orchestration.workflows.durable import WorkflowRunPhase


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    reference: WorkflowRunReference
    run: WorkflowRun = field(repr=False)
    graph_meta: WorkflowRunMetadata | None = field(default=None, repr=False)
    status: WorkflowRunPhase = WorkflowRunPhase.CREATED

    @property
    def run_state(self):
        return self.graph_meta.run_state if self.graph_meta is not None else None

    @property
    def state_snapshot(self):
        return self.graph_meta.state if self.graph_meta is not None else None

    @property
    def command_name(self) -> str:
        metadata = self.graph_meta
        if metadata is None:
            return str(self.reference.definition_id)
        return str(metadata.graph_ref.command_name)

    @property
    def completed_nodes(self) -> set[str]:
        run_state = self.run_state
        return set(run_state.completed_names()) if run_state is not None else set()


__all__ = ["WorkflowRunView"]
