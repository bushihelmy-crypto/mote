"""Canonical durable Agent turn-queue identities and state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from mote.contracts.agent.capacity import TurnCapacityPermitReceipt
from mote.contracts.clock import AbsoluteInstant


class TurnPriority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    URGENT = 3


class TurnQueueState(StrEnum):
    ACCEPTED = "accepted"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    RETRY_EXHAUSTED = "retry_exhausted"

    @property
    def terminal(self) -> bool:
        return self in {
            TurnQueueState.SUCCEEDED,
            TurnQueueState.FAILED,
            TurnQueueState.CANCELLED,
            TurnQueueState.EXPIRED,
            TurnQueueState.RETRY_EXHAUSTED,
        }


class TurnAdmissionDisposition(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    BACKPRESSURED = "backpressured"
    REJECTED_CAPACITY = "rejected_capacity"
    CONFLICT = "conflict"


class TurnMutationDisposition(StrEnum):
    APPLIED = "applied"
    ALREADY_TERMINAL = "already_terminal"
    REVISION_CONFLICT = "revision_conflict"
    STALE_FENCE = "stale_fence"
    OWNER_LOST = "owner_lost"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class TurnSchedulingDeficit:
    root_id: str
    subtree_id: str | None
    units: int

    def __post_init__(self) -> None:
        if type(self.root_id) is not str or not self.root_id:
            raise ValueError("turn scheduling root identity is invalid")
        if self.subtree_id is not None and (type(self.subtree_id) is not str or not self.subtree_id):
            raise ValueError("turn scheduling subtree identity is invalid")
        if type(self.units) is not int or self.units < 0:
            raise ValueError("turn scheduling deficit is invalid")


@dataclass(frozen=True, slots=True)
class TurnSubtreeCursor:
    root_id: str
    subtree_id: str

    def __post_init__(self) -> None:
        if type(self.root_id) is not str or not self.root_id:
            raise ValueError("turn subtree cursor root identity is invalid")
        if type(self.subtree_id) is not str or not self.subtree_id:
            raise ValueError("turn subtree cursor identity is invalid")


@dataclass(frozen=True, slots=True)
class TurnSchedulingCursor:
    root_id: str | None
    subtrees: tuple[TurnSubtreeCursor, ...]

    def __post_init__(self) -> None:
        if self.root_id is not None and (type(self.root_id) is not str or not self.root_id):
            raise ValueError("turn scheduling cursor identity is invalid")
        if not isinstance(self.subtrees, tuple) or not all(
            isinstance(value, TurnSubtreeCursor) for value in self.subtrees
        ):
            raise TypeError("turn subtree cursors must be a typed tuple")
        roots = tuple(value.root_id for value in self.subtrees)
        if len(roots) != len(set(roots)):
            raise ValueError("turn subtree cursors must have unique roots")


@dataclass(frozen=True, slots=True)
class TurnSchedulingState:
    config_generation: int
    cursor: TurnSchedulingCursor
    deficits: tuple[TurnSchedulingDeficit, ...]

    def __post_init__(self) -> None:
        if type(self.config_generation) is not int or self.config_generation < 0:
            raise ValueError("turn scheduling config generation is invalid")
        if not isinstance(self.cursor, TurnSchedulingCursor):
            raise TypeError("turn scheduling cursor is invalid")
        if not isinstance(self.deficits, tuple) or not all(
            isinstance(value, TurnSchedulingDeficit) for value in self.deficits
        ):
            raise TypeError("turn scheduling deficits must be a typed tuple")
        identities = tuple((value.root_id, value.subtree_id) for value in self.deficits)
        if len(identities) != len(set(identities)):
            raise ValueError("turn scheduling deficit identities must be unique")


EMPTY_TURN_SCHEDULING_STATE = TurnSchedulingState(
    config_generation=0,
    cursor=TurnSchedulingCursor(None, ()),
    deficits=(),
)


@dataclass(frozen=True, slots=True)
class TurnQueueIdentity:
    queue_id: str
    request_id: str
    root_id: str
    subtree_id: str
    agent_id: str
    delivery_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "queue_id",
            "request_id",
            "root_id",
            "subtree_id",
            "agent_id",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"turn queue {name} must be a non-empty string")
        if (
            not isinstance(self.delivery_ids, tuple)
            or not self.delivery_ids
            or any(type(value) is not str or not value for value in self.delivery_ids)
            or len(set(self.delivery_ids)) != len(self.delivery_ids)
        ):
            raise ValueError("turn queue delivery identities must be a unique non-empty tuple")


@dataclass(frozen=True, slots=True)
class TurnClaimBinding:
    scheduler_subject: str
    scheduler_owner_id: str
    scheduler_fencing_token: int
    process_instance_id: str
    execution_permit_receipt: TurnCapacityPermitReceipt
    queue_revision: int
    claimed_at: AbsoluteInstant

    def __post_init__(self) -> None:
        for name in ("scheduler_subject", "scheduler_owner_id", "process_instance_id"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"turn claim {name} must be a non-empty string")
        for name in ("scheduler_fencing_token", "queue_revision"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"turn claim {name} must be a positive integer")
        if not isinstance(self.execution_permit_receipt, TurnCapacityPermitReceipt):
            raise TypeError("turn claim execution permit receipt is invalid")
        if not isinstance(self.claimed_at, AbsoluteInstant):
            raise TypeError("turn claim instant must be AbsoluteInstant")


@dataclass(frozen=True, slots=True)
class TurnQueueItem:
    identity: TurnQueueIdentity
    enqueue_sequence: int
    config_generation: int
    revision: int
    priority: TurnPriority
    state: TurnQueueState
    accepted_at: AbsoluteInstant
    deadline: AbsoluteInstant | None
    attempt: int
    maximum_attempts: int
    next_eligible_at: AbsoluteInstant | None
    claim: TurnClaimBinding | None = None
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, TurnQueueIdentity):
            raise TypeError("turn queue identity is invalid")
        for name in ("enqueue_sequence", "config_generation", "revision", "maximum_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"turn queue {name} must be a positive integer")
        if type(self.attempt) is not int or self.attempt < 0 or self.attempt > self.maximum_attempts:
            raise ValueError("turn queue attempt is outside its bounded range")
        if not isinstance(self.priority, TurnPriority):
            raise TypeError("turn queue priority is invalid")
        if not isinstance(self.state, TurnQueueState):
            raise TypeError("turn queue state is invalid")
        if not isinstance(self.accepted_at, AbsoluteInstant):
            raise TypeError("turn accepted instant is invalid")
        for instant in (self.deadline, self.next_eligible_at):
            if instant is not None:
                if not isinstance(instant, AbsoluteInstant):
                    raise TypeError("turn queue optional instant is invalid")
                instant.require_clock(self.accepted_at.clock)
        if self.deadline is not None and self.accepted_at.is_at_or_after(self.deadline):
            raise ValueError("turn deadline must be after durable acceptance")
        if self.next_eligible_at is not None and not self.next_eligible_at.is_at_or_after(self.accepted_at):
            raise ValueError("turn retry eligibility precedes acceptance")
        if self.state is TurnQueueState.CLAIMED:
            if self.claim is None or self.claim.queue_revision != self.revision:
                raise ValueError("claimed turn must bind its current queue revision")
        elif self.claim is not None:
            raise ValueError("only a claimed turn may carry a claim binding")
        if self.state.terminal:
            if type(self.terminal_reason) is not str or not self.terminal_reason:
                raise ValueError("terminal turn requires a typed non-empty reason")
        elif self.terminal_reason is not None:
            raise ValueError("non-terminal turn cannot carry a terminal reason")

    @property
    def eligible(self) -> bool:
        return self.state is TurnQueueState.ACCEPTED


@dataclass(frozen=True, slots=True)
class TurnAcceptanceRequest:
    identity: TurnQueueIdentity
    config_generation: int
    priority: TurnPriority
    accepted_at: AbsoluteInstant
    deadline: AbsoluteInstant | None
    maximum_attempts: int

    def __post_init__(self) -> None:
        if not isinstance(self.identity, TurnQueueIdentity):
            raise TypeError("turn acceptance identity is invalid")
        if type(self.config_generation) is not int or self.config_generation < 1:
            raise ValueError("turn acceptance config generation is invalid")
        if not isinstance(self.priority, TurnPriority):
            raise TypeError("turn acceptance priority is invalid")
        if not isinstance(self.accepted_at, AbsoluteInstant):
            raise TypeError("turn acceptance instant is invalid")
        if self.deadline is not None:
            if not isinstance(self.deadline, AbsoluteInstant):
                raise TypeError("turn acceptance deadline is invalid")
            if self.accepted_at.is_at_or_after(self.deadline):
                raise ValueError("turn acceptance deadline must be in the future")
        if type(self.maximum_attempts) is not int or self.maximum_attempts < 1:
            raise ValueError("turn acceptance attempt bound is invalid")


@dataclass(frozen=True, slots=True)
class TurnAdmissionReceipt:
    disposition: TurnAdmissionDisposition
    request_id: str
    queue_id: str
    revision: int | None


@dataclass(frozen=True, slots=True)
class TurnMutationReceipt:
    disposition: TurnMutationDisposition
    queue_id: str
    request_id: str
    revision: int | None
    state: TurnQueueState | None


__all__ = [
    "TurnAdmissionDisposition",
    "TurnAdmissionReceipt",
    "TurnAcceptanceRequest",
    "TurnClaimBinding",
    "TurnMutationDisposition",
    "TurnMutationReceipt",
    "TurnPriority",
    "TurnQueueIdentity",
    "TurnQueueItem",
    "TurnQueueState",
    "TurnSchedulingCursor",
    "TurnSchedulingDeficit",
    "TurnSchedulingState",
    "TurnSubtreeCursor",
    "EMPTY_TURN_SCHEDULING_STATE",
]
