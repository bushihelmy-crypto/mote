"""Typed durable root/subtree cancellation delivery contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, LineageRevision
from mote.contracts.workflow.authority import WorkflowCreateAdmissionId
from mote.contracts.workflow.identity import WorkflowRunReference

MAX_WORKFLOW_GOVERNANCE_TARGETS = 1024


class WorkflowGovernanceScopeCancelRequestId(str):
    def __new__(cls, value: str) -> "WorkflowGovernanceScopeCancelRequestId":
        if type(value) is not str or not value:
            raise ValueError("Workflow governance request identity must be non-empty")
        return str.__new__(cls, value)


class WorkflowGovernanceCancelReason(str, Enum):
    ROOT_CANCELLATION = "root_cancellation"


@dataclass(frozen=True, slots=True)
class WorkflowGovernanceCancelRequest:
    request_id: WorkflowGovernanceScopeCancelRequestId
    root_agent_id: AgentId
    subtree_agent_id: AgentId
    lineage_snapshot_revision: LineageRevision
    cancellation_epoch: CancellationEpoch
    target_agent_ids: tuple[AgentId, ...]
    admitted_workflow_create_ids: tuple[WorkflowCreateAdmissionId, ...]
    reason: WorkflowGovernanceCancelReason = WorkflowGovernanceCancelReason.ROOT_CANCELLATION

    def __post_init__(self) -> None:
        if not all(isinstance(value, AgentId) for value in (self.root_agent_id, self.subtree_agent_id)):
            raise TypeError("Workflow governance scope identities must be AgentId")
        if not isinstance(self.lineage_snapshot_revision, LineageRevision):
            raise TypeError("Workflow governance snapshot revision is invalid")
        if not isinstance(self.cancellation_epoch, CancellationEpoch):
            raise TypeError("Workflow governance cancellation epoch is invalid")
        if type(self.target_agent_ids) is not tuple or not self.target_agent_ids:
            raise ValueError("Workflow governance frozen targets are required")
        if any(not isinstance(value, AgentId) for value in self.target_agent_ids):
            raise TypeError("Workflow governance target identity is invalid")
        if self.subtree_agent_id not in self.target_agent_ids:
            raise ValueError("Workflow governance targets must include the subtree root")
        if type(self.admitted_workflow_create_ids) is not tuple or any(
            not isinstance(value, WorkflowCreateAdmissionId) for value in self.admitted_workflow_create_ids
        ):
            raise TypeError("Workflow governance admission cutoff is invalid")
        if len(self.target_agent_ids) > MAX_WORKFLOW_GOVERNANCE_TARGETS:
            raise ValueError("Workflow governance target cap exceeded")
        if len(self.admitted_workflow_create_ids) > MAX_WORKFLOW_GOVERNANCE_TARGETS:
            raise ValueError("Workflow governance admission cap exceeded")
        if len(set(self.target_agent_ids)) != len(self.target_agent_ids):
            raise ValueError("Workflow governance targets are duplicated")
        if len(set(self.admitted_workflow_create_ids)) != len(self.admitted_workflow_create_ids):
            raise ValueError("Workflow governance admissions are duplicated")
        expected = self.derive_id(self.root_agent_id, self.subtree_agent_id, self.cancellation_epoch)
        if self.request_id != expected:
            raise ValueError("Workflow governance request identity is not canonical")

    @staticmethod
    def derive_id(
        root_agent_id: AgentId,
        subtree_agent_id: AgentId,
        cancellation_epoch: CancellationEpoch,
    ) -> WorkflowGovernanceScopeCancelRequestId:
        material = f"{root_agent_id}\0{subtree_agent_id}\0{int(cancellation_epoch)}".encode()
        return WorkflowGovernanceScopeCancelRequestId("wgc_" + hashlib.sha256(material).hexdigest())


class WorkflowGovernanceAcceptanceDisposition(str, Enum):
    ACCEPTED = "accepted"
    IDEMPOTENT = "idempotent"
    SCOPE_MISMATCH = "scope_mismatch"
    STALE_EPOCH = "stale_epoch"
    BACKPRESSURED = "backpressured"
    FENCE_LOST = "fence_lost"


class WorkflowGovernanceSnapshotVerification(str, Enum):
    VERIFIED = "verified"
    SCOPE_MISMATCH = "scope_mismatch"
    STALE_EPOCH = "stale_epoch"
    FENCE_LOST = "fence_lost"


@dataclass(frozen=True, slots=True)
class WorkflowGovernanceCancelAcceptance:
    request_id: WorkflowGovernanceScopeCancelRequestId
    disposition: WorkflowGovernanceAcceptanceDisposition
    accepted_revision: int | None
    target_agent_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, WorkflowGovernanceScopeCancelRequestId):
            raise TypeError("Workflow governance acceptance identity is invalid")
        if not isinstance(self.disposition, WorkflowGovernanceAcceptanceDisposition):
            raise TypeError("Workflow governance acceptance disposition is invalid")
        if type(self.target_agent_count) is not int or self.target_agent_count < 1:
            raise ValueError("Workflow governance target count must be positive")
        accepted = self.disposition in {
            WorkflowGovernanceAcceptanceDisposition.ACCEPTED,
            WorkflowGovernanceAcceptanceDisposition.IDEMPOTENT,
        }
        if accepted:
            if type(self.accepted_revision) is not int or self.accepted_revision < 1:
                raise ValueError("accepted governance intent requires a revision")
        elif self.accepted_revision is not None:
            raise ValueError("rejected governance intent cannot have a revision")


class WorkflowGovernanceSettlementLifecycle(str, Enum):
    PENDING = "pending"
    RECONCILING = "reconciling"
    SETTLED = "settled"
    PARTIAL = "partial"  # non-terminal and retryable
    DEAD_LETTER = "dead_letter"  # terminal delivery failure


class WorkflowGovernanceRunDisposition(str, Enum):
    CANCEL_INTENT_APPLIED = "cancel_intent_applied"
    ALREADY_CANCELLING = "already_cancelling"
    ALREADY_TERMINAL = "already_terminal"
    RETRY_PENDING = "retry_pending"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class WorkflowGovernanceRunSettlement:
    reference: WorkflowRunReference
    per_run_request_id: str
    revision: int
    disposition: WorkflowGovernanceRunDisposition

    def __post_init__(self) -> None:
        if not isinstance(self.reference, WorkflowRunReference):
            raise TypeError("Workflow governance settlement reference is invalid")
        if type(self.per_run_request_id) is not str or not self.per_run_request_id:
            raise ValueError("Workflow governance per-run request identity is required")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Workflow governance run revision must be positive")
        if not isinstance(self.disposition, WorkflowGovernanceRunDisposition):
            raise TypeError("Workflow governance run disposition is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowGovernanceCancelSettlementSnapshot:
    request_id: WorkflowGovernanceScopeCancelRequestId
    revision: int
    lifecycle: WorkflowGovernanceSettlementLifecycle
    per_run_settlements: tuple[WorkflowGovernanceRunSettlement, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, WorkflowGovernanceScopeCancelRequestId):
            raise TypeError("Workflow governance settlement identity is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Workflow governance settlement revision must be positive")
        if not isinstance(self.lifecycle, WorkflowGovernanceSettlementLifecycle):
            raise TypeError("Workflow governance settlement lifecycle is invalid")
        if type(self.per_run_settlements) is not tuple or any(
            not isinstance(value, WorkflowGovernanceRunSettlement) for value in self.per_run_settlements
        ):
            raise TypeError("Workflow governance run settlements must be typed tuple")
        references = tuple(value.reference for value in self.per_run_settlements)
        if len(set(references)) != len(references):
            raise ValueError("Workflow governance run settlement is duplicated")


__all__ = [
    "MAX_WORKFLOW_GOVERNANCE_TARGETS",
    "WorkflowGovernanceAcceptanceDisposition",
    "WorkflowGovernanceCancelAcceptance",
    "WorkflowGovernanceCancelReason",
    "WorkflowGovernanceCancelRequest",
    "WorkflowGovernanceCancelSettlementSnapshot",
    "WorkflowGovernanceRunDisposition",
    "WorkflowGovernanceRunSettlement",
    "WorkflowGovernanceScopeCancelRequestId",
    "WorkflowGovernanceSettlementLifecycle",
    "WorkflowGovernanceSnapshotVerification",
]
