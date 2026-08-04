"""Closed durable lifecycle and deletion contracts for one Session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from mote.contracts.session.identity import SessionId


def _instant(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Session lifecycle instant must be timezone-aware")


class SessionLifecycleState(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    RECOVERY = "recovery"
    TERMINAL = "terminal"
    DELETING = "deleting"
    TOMBSTONED = "tombstoned"


class SessionBlockerKind(StrEnum):
    EFFECT = "effect"
    DELIVERY = "delivery"
    WORKFLOW = "workflow"
    APPROVAL = "approval"
    BACKGROUND_TASK = "background_task"
    RECOVERY = "recovery"


class SessionDeletionState(StrEnum):
    REQUESTED = "requested"
    CLAIMED = "claimed"
    REFERENCES_RELEASING = "references_releasing"
    METADATA_TOMBSTONED = "metadata_tombstoned"
    BLOBS_RECLAIMING = "blobs_reclaiming"
    DIRECTORY_RETIRING = "directory_retiring"
    SETTLED = "settled"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class SessionEligibilitySnapshot:
    session_id: SessionId
    lifecycle_generation: int
    revision: int
    state: SessionLifecycleState
    blockers: tuple[SessionBlockerKind, ...]
    terminal_at: datetime | None

    def __post_init__(self) -> None:
        if type(self.lifecycle_generation) is not int or self.lifecycle_generation < 1:
            raise ValueError("Session lifecycle generation is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Session lifecycle revision is invalid")
        if tuple(sorted(set(self.blockers), key=lambda item: item.value)) != self.blockers:
            raise ValueError("Session blockers must be unique and sorted")
        if self.terminal_at is not None:
            _instant(self.terminal_at)


@dataclass(frozen=True, slots=True)
class SessionDeletionCommand:
    command_id: str
    session_id: SessionId
    authority_id: str
    requested_at: datetime
    expected_lifecycle_generation: int
    expected_revision: int

    def __post_init__(self) -> None:
        if not self.command_id or not self.authority_id:
            raise ValueError("Session deletion identity is invalid")
        _instant(self.requested_at)
        if self.expected_lifecycle_generation < 1 or self.expected_revision < 1:
            raise ValueError("Session deletion expectation is invalid")


@dataclass(frozen=True, slots=True)
class SessionDeletionClaim:
    command_id: str
    session_id: SessionId
    lifecycle_generation: int
    revision: int
    owner_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        if not self.command_id or not self.owner_id:
            raise ValueError("Session deletion claim identity is invalid")
        if self.lifecycle_generation < 1 or self.revision < 1 or self.fencing_token < 1:
            raise ValueError("Session deletion claim epoch is invalid")


@dataclass(frozen=True, slots=True)
class SessionDeletionReceipt:
    command_id: str
    session_id: SessionId
    state: SessionDeletionState
    revision: int
    updated_at: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.command_id or self.revision < 1 or not isinstance(self.state, SessionDeletionState):
            raise ValueError("Session deletion receipt is invalid")
        _instant(self.updated_at)
        if type(self.detail) is not str or len(self.detail) > 4096:
            raise ValueError("Session deletion receipt detail is invalid")


SESSION_STREAM_RETENTION_SECONDS = 30 * 24 * 60 * 60
SESSION_TOMBSTONE_RETENTION_SECONDS = 365 * 24 * 60 * 60


__all__ = [
    "SESSION_STREAM_RETENTION_SECONDS",
    "SESSION_TOMBSTONE_RETENTION_SECONDS",
    "SessionBlockerKind",
    "SessionDeletionClaim",
    "SessionDeletionCommand",
    "SessionDeletionReceipt",
    "SessionDeletionState",
    "SessionEligibilitySnapshot",
    "SessionLifecycleState",
]
