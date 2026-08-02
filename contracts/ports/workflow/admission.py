"""Agent-governance commands consumed by the Workflow create coordinator."""

from typing import Protocol

from mote.contracts.workflow.admission import (
    ClaimWorkflowCreateAdmission,
    ReserveWorkflowCreateAdmission,
    SettleWorkflowCreateAdmission,
    WorkflowCreateAdmission,
    WorkflowCreateAdmissionReceipt,
)
from mote.contracts.workflow.authority import WorkflowCreateAdmissionId


class WorkflowCreateAdmissionPort(Protocol):
    def get_workflow_create_admission(
        self, admission_id: WorkflowCreateAdmissionId
    ) -> WorkflowCreateAdmission | None: ...

    def reserved_workflow_create_admissions(
        self,
    ) -> tuple[WorkflowCreateAdmission, ...]: ...

    def claim_workflow_create_admission(
        self, command: ClaimWorkflowCreateAdmission
    ) -> WorkflowCreateAdmissionReceipt: ...

    def reserve_workflow_create_admission(
        self, command: ReserveWorkflowCreateAdmission
    ) -> WorkflowCreateAdmissionReceipt: ...

    def settle_workflow_create_admission(
        self, command: SettleWorkflowCreateAdmission
    ) -> WorkflowCreateAdmissionReceipt: ...


__all__ = ["WorkflowCreateAdmissionPort"]
