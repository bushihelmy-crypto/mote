"""Durable Agent-governance reservation for Workflow creation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, LineageRevision
from mote.contracts.runtime.operation_ownership import OperationOwnership
from mote.contracts.workflow.authority import WorkflowCallerContext, WorkflowCreateAdmissionId
from mote.contracts.workflow.identity import WorkflowRunReference


class WorkflowCreateAdmissionLifecycle(str, Enum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    ABORTED = "aborted"


class WorkflowCreateAdmissionDisposition(str, Enum):
    RESERVED = "reserved"
    CLAIMED = "claimed"
    SETTLED = "settled"
    IDEMPOTENT = "idempotent"
    PREVIOUS_ADMISSION_ABORTED = "previous_admission_aborted"
    IDENTITY_CONFLICT = "identity_conflict"
    STALE_REVISION = "stale_revision"
    FENCE_LOST = "fence_lost"


@dataclass(frozen=True, slots=True)
class WorkflowCreateAdmission:
    admission_id: WorkflowCreateAdmissionId
    create_request_id: str
    reference: WorkflowRunReference
    logical_agent_id: AgentId
    root_agent_id: AgentId
    lineage_revision: LineageRevision
    cancellation_epoch: CancellationEpoch
    revision: int
    ownership: OperationOwnership
    lifecycle: WorkflowCreateAdmissionLifecycle

    def __post_init__(self) -> None:
        if not self.create_request_id:
            raise ValueError("Workflow admission request identity is required")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Workflow admission revision must be positive")
        if self.ownership.request.operation_id != str(self.admission_id):
            raise ValueError("Workflow admission ownership identity is inconsistent")


@dataclass(frozen=True, slots=True)
class WorkflowCreateAdmissionReceipt:
    disposition: WorkflowCreateAdmissionDisposition
    admission: WorkflowCreateAdmission | None


@dataclass(frozen=True, slots=True)
class ReserveWorkflowCreateAdmission:
    admission_id: WorkflowCreateAdmissionId
    create_request_id: str
    reference: WorkflowRunReference
    caller: WorkflowCallerContext
    cancellation_epoch: CancellationEpoch
    ownership: OperationOwnership


@dataclass(frozen=True, slots=True)
class SettleWorkflowCreateAdmission:
    admission_id: WorkflowCreateAdmissionId
    lifecycle: WorkflowCreateAdmissionLifecycle
    expected_revision: int
    ownership: OperationOwnership


@dataclass(frozen=True, slots=True)
class ClaimWorkflowCreateAdmission:
    admission_id: WorkflowCreateAdmissionId
    expected_revision: int
    ownership: OperationOwnership


__all__ = [
    "WorkflowCreateAdmission",
    "WorkflowCreateAdmissionDisposition",
    "WorkflowCreateAdmissionLifecycle",
    "WorkflowCreateAdmissionReceipt",
    "ReserveWorkflowCreateAdmission",
    "SettleWorkflowCreateAdmission",
    "ClaimWorkflowCreateAdmission",
]
