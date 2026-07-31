import asyncio
from datetime import datetime, timezone

from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore
from mote.product.interfaces.inference_inspector_api import TrafficInspectorProjection

DIGEST = "sha256:" + "d" * 64


def test_traffic_inspector_is_read_only_and_redacted(tmp_path):
    async def scenario():
        store = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await store.initialize()
        await store.accept(
            AttemptReceipt(
                attempt_id="execution",
                generation_id="generation",
                generation_artifact_digest=DIGEST,
                revision=1,
                state=ReceiptState.ACCEPTED,
                fencing_token=1,
                request_digest=DIGEST,
                operation="chat.complete",
                idempotency_class="attempt",
                provider_request_id="provider-secret-id",
                updated_at=datetime.now(timezone.utc),
            )
        )
        inspector = TrafficInspectorProjection(store)
        item = await inspector.get("execution", "generation")
        assert item is not None
        assert item.cursor.endswith(":execution:generation:1")
        assert item.provider_request_present is True
        assert "provider-secret-id" not in repr(item)
        assert not hasattr(inspector, "replay")
        assert await inspector.list(state=ReceiptState.ACCEPTED) == (item,)

    asyncio.run(scenario())
