from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState, validate_receipt_transition

DIGEST = "sha256:" + "b" * 64


def _receipt(state: ReceiptState, revision: int, **changes):
    values = {
        "attempt_id": "a",
        "generation_id": "g",
        "generation_artifact_digest": DIGEST,
        "revision": revision,
        "state": state,
        "fencing_token": 1,
        "request_digest": DIGEST,
        "operation": "chat.complete",
        "idempotency_class": "attempt",
        "updated_at": datetime.now(timezone.utc),
    }
    if state not in {ReceiptState.ACCEPTED, ReceiptState.SEND_INTENT_DURABLE}:
        values.update(permit_digest=DIGEST, permit_ordinal=1)
    values.update(changes)
    return AttemptReceipt(**values)


def test_receipt_state_is_monotonic_and_cas_revision_is_exact():
    accepted = _receipt(ReceiptState.ACCEPTED, 1)
    intent = _receipt(ReceiptState.SEND_INTENT_DURABLE, 2)
    committed = _receipt(ReceiptState.SEND_COMMITTED, 3)
    validate_receipt_transition(accepted, intent)
    validate_receipt_transition(intent, committed)
    with pytest.raises(ValueError, match="exactly one"):
        validate_receipt_transition(accepted, committed.model_copy(update={"revision": 3}))


def test_terminal_receipt_is_immutable_and_committed_state_requires_permit():
    terminal = _receipt(ReceiptState.IN_DOUBT, 4)
    with pytest.raises(ValueError, match="terminal receipt"):
        validate_receipt_transition(terminal, terminal.model_copy(update={"revision": 5}))
    with pytest.raises(ValidationError, match="requires permit"):
        _receipt(ReceiptState.SEND_COMMITTED, 2, permit_digest=None, permit_ordinal=None)
