"""Canonical lineage verification for bound Workflow callers."""

from mote.contracts.agent.lineage import LineageAuthorizationDisposition
from mote.contracts.agent.runtime_identity import LineageRevision
from mote.contracts.workflow.authority import (
    WorkflowCallerAuthorizationDisposition,
    WorkflowCallerAuthorizationReceipt,
    WorkflowCallerContext,
)

from .store import AgentLineageStore


class AgentLineageWorkflowCallerAuthorizer:
    def __init__(self, lineage: AgentLineageStore) -> None:
        self._lineage = lineage

    def authorize_workflow_caller(self, caller: WorkflowCallerContext) -> WorkflowCallerAuthorizationReceipt:
        record = self._lineage.record_for_agent(str(caller.logical_agent_id))
        authorization = self._lineage.authorize_incarnation(
            str(caller.logical_agent_id),
            incarnation_generation=int(caller.incarnation_generation),
            fencing_token=caller.owner_fencing_token,
        )
        mapping = {
            LineageAuthorizationDisposition.NOT_FOUND: WorkflowCallerAuthorizationDisposition.NOT_FOUND,
            LineageAuthorizationDisposition.NOT_ACTIVE: WorkflowCallerAuthorizationDisposition.NOT_ACTIVE,
            LineageAuthorizationDisposition.INCARNATION_MISMATCH: WorkflowCallerAuthorizationDisposition.INCARNATION_MISMATCH,
            LineageAuthorizationDisposition.STALE_FENCE: WorkflowCallerAuthorizationDisposition.STALE_FENCE,
        }
        if authorization.disposition is not LineageAuthorizationDisposition.AUTHORIZED:
            disposition = mapping[authorization.disposition]
        elif record is None or record.request.root_agent_id != caller.root_governance_agent_id:
            disposition = WorkflowCallerAuthorizationDisposition.ROOT_MISMATCH
        else:
            disposition = WorkflowCallerAuthorizationDisposition.AUTHORIZED
        return WorkflowCallerAuthorizationReceipt(
            disposition,
            None if record is None or record.revision < 1 else LineageRevision(record.revision),
        )


__all__ = ["AgentLineageWorkflowCallerAuthorizer"]
