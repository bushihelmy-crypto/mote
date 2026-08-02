"""Strict asynchronous-work command and receipt unions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from mote.contracts.async_work.identity import DurableWorkflowRunReference, LocalBackgroundTaskReference
from mote.contracts.workflow.command import WorkflowCancelReason


@dataclass(frozen=True, slots=True)
class CancelLocalBackgroundTask:
    reference: LocalBackgroundTaskReference
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference, LocalBackgroundTaskReference):
            raise TypeError("local cancellation requires a local reference")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("local cancellation reason is required")


@dataclass(frozen=True, slots=True)
class CancelDurableWorkflowRun:
    reference: DurableWorkflowRunReference
    expected_revision: int
    reason: WorkflowCancelReason

    def __post_init__(self) -> None:
        if not isinstance(self.reference, DurableWorkflowRunReference):
            raise TypeError("Workflow cancellation requires a durable reference")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("Workflow cancellation revision must be positive")
        if not isinstance(self.reason, WorkflowCancelReason):
            raise TypeError("Workflow cancellation reason is invalid")


CancelAsyncWork: TypeAlias = CancelLocalBackgroundTask | CancelDurableWorkflowRun


class LocalCancelDisposition(str, Enum):
    CANCEL_REQUESTED = "cancel_requested"
    ALREADY_TERMINAL = "already_terminal"
    OWNER_LOST = "owner_lost"
    INCARNATION_LOST = "incarnation_lost"
    STALE_ATTEMPT = "stale_attempt"
    NOT_FOUND = "not_found"


class WorkflowCancelDisposition(str, Enum):
    CANCEL_REQUESTED = "cancel_requested"
    ALREADY_CANCELLING = "already_cancelling"
    ALREADY_TERMINAL = "already_terminal"
    REVISION_CONFLICT = "revision_conflict"
    PRINCIPAL_MISMATCH = "principal_mismatch"
    CALLER_NOT_ACTIVE = "caller_not_active"
    INCARNATION_MISMATCH = "incarnation_mismatch"
    LINEAGE_REVISION_STALE = "lineage_revision_stale"
    DEFINITION_MISMATCH = "definition_mismatch"
    CONTROL_UNAVAILABLE = "control_unavailable"
    CLAIM_CONFLICT = "claim_conflict"
    FENCE_LOST = "fence_lost"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class LocalCancelReceipt:
    reference: LocalBackgroundTaskReference
    disposition: LocalCancelDisposition


@dataclass(frozen=True, slots=True)
class WorkflowCancelReceipt:
    reference: DurableWorkflowRunReference
    disposition: WorkflowCancelDisposition
    revision: int | None


@dataclass(frozen=True, slots=True)
class ResumeDurableWorkflowRun:
    reference: DurableWorkflowRunReference
    expected_revision: int
    resume_nonce: str
    checkpoint_payload: str
    frontier: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reference, DurableWorkflowRunReference):
            raise TypeError("Workflow resume requires a durable reference")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("Workflow resume revision must be positive")
        if type(self.resume_nonce) is not str or not self.resume_nonce:
            raise ValueError("Workflow resume nonce is required")
        if type(self.checkpoint_payload) is not str or not self.checkpoint_payload:
            raise ValueError("Workflow resume checkpoint is required")
        if any(type(node) is not str or not node for node in self.frontier):
            raise ValueError("Workflow resume frontier is invalid")


class WorkflowResumeDisposition(str, Enum):
    RESUMED = "resumed"
    NOT_PAUSED = "not_paused"
    REVISION_CONFLICT = "revision_conflict"
    PRINCIPAL_MISMATCH = "principal_mismatch"
    CALLER_NOT_ACTIVE = "caller_not_active"
    INCARNATION_MISMATCH = "incarnation_mismatch"
    LINEAGE_REVISION_STALE = "lineage_revision_stale"
    DEFINITION_MISMATCH = "definition_mismatch"
    CONTROL_UNAVAILABLE = "control_unavailable"
    CLAIM_CONFLICT = "claim_conflict"
    FENCE_LOST = "fence_lost"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class WorkflowResumeReceipt:
    reference: DurableWorkflowRunReference
    disposition: WorkflowResumeDisposition
    revision: int | None


__all__ = [
    "CancelAsyncWork",
    "CancelDurableWorkflowRun",
    "CancelLocalBackgroundTask",
    "LocalCancelDisposition",
    "LocalCancelReceipt",
    "WorkflowCancelDisposition",
    "WorkflowCancelReceipt",
    "ResumeDurableWorkflowRun",
    "WorkflowResumeDisposition",
    "WorkflowResumeReceipt",
]
