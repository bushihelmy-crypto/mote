from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from mote.contracts.inference.base import FrozenContract


class AttemptEventType(StrEnum):
    QUEUED = "queued"
    BUDGET_RESERVED = "budget_reserved"
    DISPATCHED = "dispatched"
    WIRE_PREPARED = "wire_prepared"
    WIRE_AUTHORIZATION_REQUIRED = "wire_authorization_required"
    SEND_COMMITTED = "send_committed"
    WIRE_STARTED = "wire_started"
    RESPONSE_STARTED = "response_started"
    STREAM_CHUNK = "stream_chunk"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    IN_DOUBT = "in_doubt"


TERMINAL_ATTEMPT_EVENTS = frozenset(
    {
        AttemptEventType.SUCCEEDED,
        AttemptEventType.FAILED,
        AttemptEventType.CANCELLED,
        AttemptEventType.IN_DOUBT,
    }
)


class AttemptLifecycleEvent(FrozenContract):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    receipt_revision: int = Field(ge=1)
    generation_id: str = Field(min_length=1)
    event_type: AttemptEventType
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.event_type in TERMINAL_ATTEMPT_EVENTS


class SessionEventType(StrEnum):
    QUEUED = "queued"
    OPEN_AUTHORIZATION_REQUIRED = "open_authorization_required"
    OPENED = "opened"
    MESSAGE_SENT = "message_sent"
    MESSAGE_RECEIVED = "message_received"
    CLOSED = "closed"
    FAILED = "failed"
    IN_DOUBT = "in_doubt"


class SessionLifecycleEvent(FrozenContract):
    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    receipt_revision: int = Field(ge=1)
    generation_id: str = Field(min_length=1)
    event_type: SessionEventType
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.event_type in {
            SessionEventType.CLOSED,
            SessionEventType.FAILED,
            SessionEventType.IN_DOUBT,
        }
