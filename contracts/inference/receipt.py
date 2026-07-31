from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from mote.contracts.inference.base import FrozenContract


class ReceiptState(StrEnum):
    ACCEPTED = "accepted"
    SEND_INTENT_DURABLE = "send_intent_durable"
    SEND_COMMITTED = "send_committed"
    WIRE_STARTED_OBSERVED = "wire_started_observed"
    PROVIDER_ACK = "provider_ack"
    TERMINAL_SUCCEEDED = "terminal_succeeded"
    TERMINAL_FAILED = "terminal_failed"
    TERMINAL_CANCELLED = "terminal_cancelled"
    IN_DOUBT = "in_doubt"


TERMINAL_RECEIPT_STATES = frozenset(
    {
        ReceiptState.TERMINAL_SUCCEEDED,
        ReceiptState.TERMINAL_FAILED,
        ReceiptState.TERMINAL_CANCELLED,
        ReceiptState.IN_DOUBT,
    }
)

RECEIPT_TRANSITIONS = {
    ReceiptState.ACCEPTED: frozenset(
        {
            ReceiptState.SEND_INTENT_DURABLE,
            ReceiptState.TERMINAL_FAILED,
            ReceiptState.TERMINAL_CANCELLED,
        }
    ),
    ReceiptState.SEND_INTENT_DURABLE: frozenset(
        {
            ReceiptState.SEND_COMMITTED,
            ReceiptState.TERMINAL_FAILED,
            ReceiptState.TERMINAL_CANCELLED,
        }
    ),
    ReceiptState.SEND_COMMITTED: frozenset(
        {
            ReceiptState.WIRE_STARTED_OBSERVED,
            ReceiptState.TERMINAL_FAILED,
            ReceiptState.TERMINAL_CANCELLED,
            ReceiptState.IN_DOUBT,
        }
    ),
    ReceiptState.WIRE_STARTED_OBSERVED: frozenset(
        {
            ReceiptState.PROVIDER_ACK,
            ReceiptState.TERMINAL_SUCCEEDED,
            ReceiptState.TERMINAL_FAILED,
            ReceiptState.TERMINAL_CANCELLED,
            ReceiptState.IN_DOUBT,
        }
    ),
    ReceiptState.PROVIDER_ACK: TERMINAL_RECEIPT_STATES,
}


class AttemptReceipt(FrozenContract):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    generation_artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    state: ReceiptState
    fencing_token: int = Field(ge=1)
    permit_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    permit_ordinal: int | None = Field(default=None, ge=1)
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    operation: str = Field(min_length=1)
    idempotency_class: str = Field(min_length=1)
    provider_request_id: str | None = None
    terminal_artifact_reference: str | None = None
    terminal_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _committed_receipt_has_permit_binding(self) -> "AttemptReceipt":
        committed = self.state not in {ReceiptState.ACCEPTED, ReceiptState.SEND_INTENT_DURABLE}
        if committed and (self.permit_digest is None or self.permit_ordinal is None):
            raise ValueError("committed receipt requires permit digest and ordinal")
        if self.updated_at.utcoffset() is None:
            raise ValueError("receipt timestamp must be timezone-aware")
        return self


def validate_receipt_transition(previous: AttemptReceipt, current: AttemptReceipt) -> None:
    if (previous.attempt_id, previous.generation_id) != (current.attempt_id, current.generation_id):
        raise ValueError("receipt identity cannot change")
    if current.revision != previous.revision + 1:
        raise ValueError("receipt revision must advance by exactly one")
    if current.fencing_token < previous.fencing_token:
        raise ValueError("receipt fencing token cannot regress")
    if previous.state in TERMINAL_RECEIPT_STATES:
        raise ValueError("terminal receipt cannot transition")
    if current.state not in RECEIPT_TRANSITIONS[previous.state]:
        raise ValueError(f"illegal receipt transition {previous.state} -> {current.state}")
