import asyncio
from datetime import datetime, timezone

from mote.contracts.inference.provider_evidence import ProviderEvidence
from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore, SQLiteReconciliationAuthority
from mote.product.inference.daemon.admin_projection import build_daemon_admin_read_model

DIGEST = "sha256:" + "a" * 64


class _Audit:
    async def read(self, *, after=0):
        yield {"sequence": after + 1, "operation": "reconcile_all"}


def test_daemon_admin_model_reads_restart_durable_receipts_and_backlog(tmp_path):
    async def scenario():
        store = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await store.initialize()
        await store.accept(
            AttemptReceipt(
                attempt_id="execution",
                generation_id="generation",
                generation_artifact_digest=DIGEST,
                revision=1,
                state=ReceiptState.IN_DOUBT,
                fencing_token=1,
                permit_digest=DIGEST,
                permit_ordinal=1,
                request_digest=DIGEST,
                operation="batch.create",
                idempotency_class="durable_command",
                updated_at=datetime.now(timezone.utc),
            )
        )
        model = build_daemon_admin_read_model(
            receipts=store,
            readiness=lambda: (False, {"admission": "closed"}),
            audit=_Audit(),
        )
        return (
            await model.receipt("execution"),
            await model.reconciliation(),
            await model.readiness(),
            await model.audit(4),
        )

    receipt, backlog, readiness, audit = asyncio.run(scenario())
    assert receipt.execution_id == "execution"
    assert receipt.state == "in_doubt"
    assert backlog[0]["execution_id"] == "execution"
    assert readiness == {"ready": False, "components": {"admission": "closed"}}
    assert audit[0]["sequence"] == 5


def test_daemon_admin_model_projects_redacted_owner_action(tmp_path):
    async def scenario():
        store = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await store.initialize()
        authority = SQLiteReconciliationAuthority(store)
        await authority.bind_execution(
            execution_id="execution",
            generation_id="generation",
            provider="openai",
            owner_id="owner",
            strategy_id="verified-webhook-v1",
        )
        await authority.append(
            ProviderEvidence(
                provider="openai",
                event_id="event",
                execution_id="execution",
                event_type="completed",
                occurred_at=datetime.now(timezone.utc),
                provider_resource_id="must-not-leak",
            )
        )
        await authority.propose("execution")
        model = build_daemon_admin_read_model(
            receipts=store,
            readiness=lambda: (True, {}),
            audit=_Audit(),
            reconciliation_authority=authority,
        )
        return await model.reconciliation()

    backlog = asyncio.run(scenario())
    assert backlog[0]["state"] == "owner_action_required"
    assert backlog[0]["evidence_count"] == 1
    assert "evidence_digests" not in backlog[0]
    assert "provider_resource_id" not in backlog[0]
