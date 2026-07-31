import asyncio
from datetime import datetime, timezone

from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore
from mote.runtime.inference.receipt_projection import ReceiptProjection

DIGEST = "sha256:" + "a" * 64


def _receipt(attempt_id, state, revision):
    committed = state not in {ReceiptState.ACCEPTED, ReceiptState.SEND_INTENT_DURABLE}
    return AttemptReceipt(
        attempt_id=attempt_id,
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        revision=revision,
        state=state,
        fencing_token=1,
        permit_digest=DIGEST if committed else None,
        permit_ordinal=1 if committed else None,
        request_digest=DIGEST,
        operation="batch.create",
        idempotency_class="durable_command",
        provider_request_id="provider-secret",
        updated_at=datetime.now(timezone.utc),
    )


def test_receipt_projection_queries_identity_and_lists_only_in_doubt(tmp_path):
    async def scenario():
        store = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await store.initialize()
        await store.accept(_receipt("open", ReceiptState.ACCEPTED, 1))
        accepted = await store.accept(_receipt("doubt", ReceiptState.ACCEPTED, 1))
        intent = accepted.model_copy(
            update={
                "revision": 2,
                "state": ReceiptState.SEND_INTENT_DURABLE,
            }
        )
        await store.compare_and_swap(intent, expected_revision=1, fencing_token=1)
        committed = intent.model_copy(
            update={
                "revision": 3,
                "state": ReceiptState.SEND_COMMITTED,
                "permit_digest": DIGEST,
                "permit_ordinal": 1,
            }
        )
        await store.compare_and_swap(committed, expected_revision=2, fencing_token=1)
        doubt = committed.model_copy(
            update={
                "revision": 4,
                "state": ReceiptState.IN_DOUBT,
            }
        )
        await store.compare_and_swap(doubt, expected_revision=3, fencing_token=1)
        projection = ReceiptProjection(store)
        return await projection.get("doubt", "generation"), await projection.reconciliation()

    receipt, backlog = asyncio.run(scenario())
    assert receipt is not None and receipt.state == "in_doubt"
    assert receipt.provider_request_present is True
    assert not hasattr(receipt, "provider_request_id")
    assert [item.execution_id for item in backlog] == ["doubt"]
