"""Consumer-owned ports for durable Workflow governance cancellation."""

from typing import Protocol

from mote.contracts.agent.runtime_identity import AgentId
from mote.contracts.ports.workflow.admission import WorkflowCreateAdmissionPort
from mote.contracts.workflow.admission import WorkflowCreateAdmission
from mote.contracts.workflow.authority import WorkflowCreateAdmissionId
from mote.contracts.workflow.governance import (
    WorkflowGovernanceCancelAcceptance,
    WorkflowGovernanceCancelRequest,
    WorkflowGovernanceCancelSettlementSnapshot,
    WorkflowGovernanceScopeCancelRequestId,
    WorkflowGovernanceSnapshotVerification,
)


class WorkflowGovernanceSnapshotVerifierPort(Protocol):
    def verify(self, request: WorkflowGovernanceCancelRequest) -> WorkflowGovernanceSnapshotVerification: ...


class WorkflowGovernanceAdmissionQueryPort(Protocol):
    def get_workflow_create_admission(
        self, admission_id: WorkflowCreateAdmissionId
    ) -> WorkflowCreateAdmission | None: ...


class WorkflowGovernanceCancellationDeliveryPort(Protocol):
    def submit(self, request: WorkflowGovernanceCancelRequest) -> WorkflowGovernanceCancelAcceptance: ...


class WorkflowGovernanceCancellationSettlementPort(Protocol):
    def get(
        self, request_id: WorkflowGovernanceScopeCancelRequestId
    ) -> WorkflowGovernanceCancelSettlementSnapshot | None: ...


class WorkflowGovernanceCompositionPort(
    WorkflowGovernanceCancellationDeliveryPort,
    WorkflowGovernanceCancellationSettlementPort,
    Protocol,
):
    def register_agent_governance(
        self,
        root_agent_id: AgentId,
        verifier: WorkflowGovernanceSnapshotVerifierPort,
        admissions: WorkflowCreateAdmissionPort,
    ) -> None: ...

    def unregister_agent_governance(self, root_agent_id: AgentId) -> None: ...


__all__ = [
    "WorkflowGovernanceCancellationDeliveryPort",
    "WorkflowGovernanceCancellationSettlementPort",
    "WorkflowGovernanceSnapshotVerifierPort",
    "WorkflowGovernanceAdmissionQueryPort",
    "WorkflowGovernanceCompositionPort",
]
