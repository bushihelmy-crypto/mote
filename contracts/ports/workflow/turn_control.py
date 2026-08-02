"""Turn-bound Workflow authority and create-admission capabilities."""

from typing import Protocol

from mote.contracts.ports.workflow.admission import WorkflowCreateAdmissionPort
from mote.contracts.ports.workflow.authority import WorkflowCallerControlPort


class WorkflowAgentTurnControlPort(WorkflowCallerControlPort, WorkflowCreateAdmissionPort, Protocol):
    pass


__all__ = ["WorkflowAgentTurnControlPort"]
