"""Frozen presentation projections; never authoritative lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from mote.contracts.async_work.identity import DurableWorkflowRunReference, LocalBackgroundTaskReference
from mote.contracts.clock import AbsoluteInstant
from mote.contracts.task.models import TaskResultPointer
from mote.contracts.workflow.result import WorkflowTerminalResult


class AsyncWorkPresentationPhase(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    OWNER_LOST = "owner_lost"


class AsyncWorkAction(str, Enum):
    CANCEL = "cancel"
    RESUME = "resume"
    RETRY = "retry"
    VIEW_RESULT = "view_result"


class WorkflowTerminalDeliveryState(str, Enum):
    AVAILABLE = "available"
    CLAIMED = "claimed"
    SETTLED = "settled"
    IN_DOUBT = "in_doubt"
    DEAD_LETTER = "dead_letter"


class WorkflowPausePresentationReason(str, Enum):
    EXTERNAL_INPUT = "external_input"
    APPROVAL = "approval"
    OPERATOR = "operator"


@dataclass(frozen=True, slots=True)
class LocalBackgroundObservationDetail:
    label: str
    owner_available: bool
    pinned: bool

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label:
            raise ValueError("local async-work label is required")
        if type(self.owner_available) is not bool or type(self.pinned) is not bool:
            raise TypeError("local async-work availability flags must be boolean")


@dataclass(frozen=True, slots=True)
class WorkflowPauseDetail:
    reason: WorkflowPausePresentationReason
    resume_nonce: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, WorkflowPausePresentationReason):
            raise TypeError("Workflow pause reason is invalid")
        if type(self.resume_nonce) is not str or not self.resume_nonce:
            raise ValueError("Workflow resume nonce is required")


@dataclass(frozen=True, slots=True)
class DurableWorkflowObservationDetail:
    pause: WorkflowPauseDetail | None

    def __post_init__(self) -> None:
        if self.pause is not None and not isinstance(self.pause, WorkflowPauseDetail):
            raise TypeError("Workflow pause detail is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowTerminalDeliveryObservation:
    delivery_id: str
    destination_id: str
    revision: int
    state: WorkflowTerminalDeliveryState
    attempts: int
    next_eligible_at: AbsoluteInstant | None
    reason: str | None

    def __post_init__(self) -> None:
        if type(self.delivery_id) is not str or not self.delivery_id:
            raise ValueError("Workflow delivery identity is required")
        if type(self.destination_id) is not str or not self.destination_id:
            raise ValueError("Workflow delivery destination is required")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Workflow delivery revision must be positive")
        if not isinstance(self.state, WorkflowTerminalDeliveryState):
            raise TypeError("Workflow delivery state is invalid")
        if type(self.attempts) is not int or self.attempts < 0:
            raise ValueError("Workflow delivery attempts must be non-negative")
        if self.next_eligible_at is not None and not isinstance(self.next_eligible_at, AbsoluteInstant):
            raise TypeError("Workflow delivery retry instant is invalid")
        if self.reason is not None and type(self.reason) is not str:
            raise TypeError("Workflow delivery reason must be text")


@dataclass(frozen=True, slots=True)
class LocalBackgroundTaskObservation:
    reference: LocalBackgroundTaskReference
    phase: AsyncWorkPresentationPhase
    detail: LocalBackgroundObservationDetail
    result_pointer: TaskResultPointer | None
    available_actions: tuple[AsyncWorkAction, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reference, LocalBackgroundTaskReference):
            raise TypeError("local observation requires local reference")
        if not isinstance(self.phase, AsyncWorkPresentationPhase):
            raise TypeError("local observation phase is invalid")
        if not isinstance(self.detail, LocalBackgroundObservationDetail):
            raise TypeError("local observation detail is invalid")
        _validate_actions(self.available_actions)


@dataclass(frozen=True, slots=True)
class DurableWorkflowRunObservation:
    reference: DurableWorkflowRunReference
    revision: int
    phase: AsyncWorkPresentationPhase
    detail: DurableWorkflowObservationDetail
    frontier: tuple[str, ...]
    deadline: AbsoluteInstant | None
    terminal_result: WorkflowTerminalResult | None
    available_actions: tuple[AsyncWorkAction, ...]
    deliveries: tuple[WorkflowTerminalDeliveryObservation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reference, DurableWorkflowRunReference):
            raise TypeError("Workflow observation requires durable reference")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Workflow observation revision must be positive")
        if not isinstance(self.phase, AsyncWorkPresentationPhase):
            raise TypeError("Workflow observation phase is invalid")
        if not isinstance(self.detail, DurableWorkflowObservationDetail):
            raise TypeError("Workflow observation detail is invalid")
        if type(self.frontier) is not tuple or any(type(node) is not str or not node for node in self.frontier):
            raise TypeError("Workflow frontier must be a tuple of node identities")
        if self.deadline is not None and not isinstance(self.deadline, AbsoluteInstant):
            raise TypeError("Workflow deadline is invalid")
        if self.terminal_result is not None and not isinstance(self.terminal_result, WorkflowTerminalResult):
            raise TypeError("Workflow terminal result is invalid")
        _validate_actions(self.available_actions)
        if type(self.deliveries) is not tuple or any(
            not isinstance(item, WorkflowTerminalDeliveryObservation) for item in self.deliveries
        ):
            raise TypeError("Workflow deliveries must be an immutable projection")


def _validate_actions(actions: tuple[AsyncWorkAction, ...]) -> None:
    if type(actions) is not tuple or any(not isinstance(action, AsyncWorkAction) for action in actions):
        raise TypeError("async-work actions must be an immutable typed tuple")
    if len(set(actions)) != len(actions):
        raise ValueError("async-work actions must not be duplicated")


AsyncWorkObservation: TypeAlias = LocalBackgroundTaskObservation | DurableWorkflowRunObservation


__all__ = [
    "AsyncWorkAction",
    "AsyncWorkObservation",
    "AsyncWorkPresentationPhase",
    "DurableWorkflowObservationDetail",
    "DurableWorkflowRunObservation",
    "LocalBackgroundObservationDetail",
    "LocalBackgroundTaskObservation",
    "WorkflowPauseDetail",
    "WorkflowPausePresentationReason",
    "WorkflowTerminalDeliveryObservation",
    "WorkflowTerminalDeliveryState",
]
