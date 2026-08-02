"""Lineage-owned verification of frozen Workflow governance cancellation scope."""

from mote.contracts.workflow.governance import WorkflowGovernanceCancelRequest, WorkflowGovernanceSnapshotVerification

from .store import AgentLineageStore


class AgentLineageWorkflowGovernanceVerifier:
    def __init__(self, lineage: AgentLineageStore) -> None:
        self._lineage = lineage

    def verify(self, request: WorkflowGovernanceCancelRequest) -> WorkflowGovernanceSnapshotVerification:
        try:
            snapshot = self._lineage.cancellation_snapshot(
                str(request.subtree_agent_id),
                cancellation_epoch=int(request.cancellation_epoch),
            )
        except (KeyError, ValueError):
            return WorkflowGovernanceSnapshotVerification.STALE_EPOCH
        if (
            snapshot.root_agent_id != request.root_agent_id
            or snapshot.subtree_agent_id != request.subtree_agent_id
            or snapshot.revision != request.lineage_snapshot_revision
            or snapshot.agent_ids != request.target_agent_ids
            or snapshot.workflow_create_admission_ids != request.admitted_workflow_create_ids
        ):
            return WorkflowGovernanceSnapshotVerification.SCOPE_MISMATCH
        return WorkflowGovernanceSnapshotVerification.VERIFIED


__all__ = ["AgentLineageWorkflowGovernanceVerifier"]
