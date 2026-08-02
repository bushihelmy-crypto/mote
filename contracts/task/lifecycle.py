"""Typed process-local ownership and drain contracts for background work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mote.contracts.task.models import AttemptId, TaskId


class BackgroundTaskPoolState(str, Enum):
    ACTIVE = "active"
    DRAINING = "draining"
    CLOSED = "closed"


class BackgroundTaskDrainDisposition(str, Enum):
    SETTLED = "settled"
    DRAINING_TIMEOUT = "draining_timeout"
    CLEANUP_FAILED = "cleanup_failed"
    OWNER_LOST = "owner_lost"


@dataclass(frozen=True, slots=True)
class BackgroundTaskOwner:
    process_instance_id: str
    agent_id: str
    incarnation_id: str

    def __post_init__(self) -> None:
        if not self.process_instance_id or not self.agent_id or not self.incarnation_id:
            raise ValueError("background-task owner identity fields must not be empty")


@dataclass(frozen=True, slots=True)
class LocalTaskReference:
    owner: BackgroundTaskOwner
    task_id: TaskId
    attempt_id: AttemptId


class BackgroundTaskAcceptance(str):
    """Typed local acceptance whose textual value is the model-visible TaskId."""

    __slots__ = ("reference",)

    reference: LocalTaskReference

    def __new__(cls, reference: LocalTaskReference) -> "BackgroundTaskAcceptance":
        instance = str.__new__(cls, str(reference.task_id))
        instance.reference = reference
        return instance


@dataclass(frozen=True, slots=True)
class BackgroundTaskPinSnapshot:
    owner: BackgroundTaskOwner
    state: BackgroundTaskPoolState
    references: tuple[LocalTaskReference, ...]

    @property
    def pin_count(self) -> int:
        return len(self.references)


@dataclass(frozen=True, slots=True)
class BackgroundTaskDrainReceipt:
    owner: BackgroundTaskOwner
    disposition: BackgroundTaskDrainDisposition
    remaining: tuple[LocalTaskReference, ...]
    failure: str | None = None

    @property
    def settled(self) -> bool:
        return self.disposition is BackgroundTaskDrainDisposition.SETTLED


class BackgroundTaskAdmissionClosed(RuntimeError):
    def __init__(self, state: BackgroundTaskPoolState) -> None:
        self.state = state
        super().__init__(f"background-task admission is {state.value}")


__all__ = [
    "BackgroundTaskAcceptance",
    "BackgroundTaskAdmissionClosed",
    "BackgroundTaskDrainDisposition",
    "BackgroundTaskDrainReceipt",
    "BackgroundTaskOwner",
    "BackgroundTaskPinSnapshot",
    "BackgroundTaskPoolState",
    "LocalTaskReference",
]
