import asyncio

import pytest

from mote.contracts.inference.session import SessionReceipt, SessionReceiptState
from mote.product.inference.backends.sqlite import (
    ReceiptConflictError,
    SQLiteAttemptReceiptStore,
    SQLiteSessionReceiptStore,
)

DIGEST = "sha256:" + "f" * 64


def test_session_receipt_cas_sequences_and_outbox_are_atomic(tmp_path):
    async def scenario():
        authority = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await authority.initialize()
        store = SQLiteSessionReceiptStore(authority)
        accepted = await store.accept(
            SessionReceipt(
                session_id="session",
                generation_id="generation",
                generation_artifact_digest=DIGEST,
                endpoint_binding_id="binding",
                revision=1,
                fencing_token=1,
                state=SessionReceiptState.ACCEPTED,
            )
        )
        committed = accepted.model_copy(
            update={
                "revision": 2,
                "state": SessionReceiptState.OPEN_SEND_COMMITTED,
                "open_permit_digest": DIGEST,
            }
        )
        assert await store.compare_and_swap(committed, expected_revision=1, fencing_token=1) == committed
        opened = committed.model_copy(update={"revision": 3, "state": SessionReceiptState.OPEN})
        await store.compare_and_swap(opened, expected_revision=2, fencing_token=1)
        advanced = opened.model_copy(update={"revision": 4, "next_outbound_sequence": 2})
        await store.compare_and_swap(advanced, expected_revision=3, fencing_token=1)
        with pytest.raises(ReceiptConflictError):
            await store.compare_and_swap(
                advanced.model_copy(update={"revision": 5}),
                expected_revision=3,
                fencing_token=1,
            )
        assert await store.get("session", "generation") == advanced

    asyncio.run(scenario())
