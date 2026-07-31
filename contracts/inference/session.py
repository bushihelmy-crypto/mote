from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import Field

from mote.contracts.inference.base import FrozenContract


class SessionReceiptState(StrEnum):
    ACCEPTED = "accepted"
    OPEN_SEND_COMMITTED = "open_send_committed"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    IN_DOUBT = "in_doubt"


TERMINAL_SESSION_STATES = frozenset(
    {
        SessionReceiptState.CLOSED,
        SessionReceiptState.FAILED,
        SessionReceiptState.IN_DOUBT,
    }
)


class SessionReceipt(FrozenContract):
    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    generation_artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    endpoint_binding_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    fencing_token: int = Field(ge=1)
    state: SessionReceiptState
    next_outbound_sequence: int = Field(default=1, ge=1)
    last_inbound_sequence: int = Field(default=0, ge=0)
    open_permit_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def validate_session_receipt_transition(previous: SessionReceipt, current: SessionReceipt) -> None:
    if (previous.session_id, previous.generation_id) != (
        current.session_id,
        current.generation_id,
    ):
        raise ValueError("session receipt identity cannot change")
    if current.revision != previous.revision + 1:
        raise ValueError("session receipt revision must advance by one")
    if current.fencing_token < previous.fencing_token:
        raise ValueError("session fencing token cannot regress")
    if previous.state in TERMINAL_SESSION_STATES:
        raise ValueError("terminal session receipt cannot transition")
    allowed = {
        SessionReceiptState.ACCEPTED: {
            SessionReceiptState.OPEN_SEND_COMMITTED,
            SessionReceiptState.FAILED,
            SessionReceiptState.CLOSED,
            SessionReceiptState.IN_DOUBT,
        },
        SessionReceiptState.OPEN_SEND_COMMITTED: {
            SessionReceiptState.OPEN,
            SessionReceiptState.IN_DOUBT,
        },
        SessionReceiptState.OPEN: {
            SessionReceiptState.OPEN,
            SessionReceiptState.CLOSING,
            SessionReceiptState.IN_DOUBT,
        },
        SessionReceiptState.CLOSING: {
            SessionReceiptState.CLOSED,
            SessionReceiptState.IN_DOUBT,
        },
    }
    if current.state not in allowed[previous.state]:
        raise ValueError(f"illegal session receipt transition {previous.state} -> {current.state}")
    if current.next_outbound_sequence < previous.next_outbound_sequence:
        raise ValueError("session outbound sequence cannot regress")
    if current.last_inbound_sequence < previous.last_inbound_sequence:
        raise ValueError("session inbound sequence cannot regress")
