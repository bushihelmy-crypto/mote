"""Consumer-owned authority verification needed by Workflow adapters."""

from typing import Protocol

from mote.contracts.agent.runtime_identity import AgentId
from mote.contracts.workflow.authority import WorkflowCallerAuthorizationReceipt, WorkflowCallerContext


class WorkflowCallerAuthorizationPort(Protocol):
    def authorize_workflow_caller(self, caller: WorkflowCallerContext) -> WorkflowCallerAuthorizationReceipt: ...


class WorkflowCallerControlPort(WorkflowCallerAuthorizationPort, Protocol):
    def workflow_caller_context(self, agent_id: AgentId) -> WorkflowCallerContext: ...


__all__ = ["WorkflowCallerAuthorizationPort", "WorkflowCallerControlPort"]
