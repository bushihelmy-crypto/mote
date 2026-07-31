import asyncio
from datetime import datetime, timezone

from mote.contracts.inference.provider_evidence import ProviderEvidence
from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.contracts.inference.reconciliation import ReconciliationState
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore, SQLiteReconciliationAuthority
from mote.runtime.inference.reconciliation_control import CallerLogicalReconciler, DaemonEvidenceReconciler

DIGEST = "sha256:" + "a" * 64


def _receipt(execution_id):
    return AttemptReceipt(
        attempt_id=execution_id,
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        revision=1,
        state=ReceiptState.IN_DOUBT,
        fencing_token=1,
        permit_digest=DIGEST,
        permit_ordinal=1,
        request_digest=DIGEST,
        operation="openai.batch.create",
        idempotency_class="durable_command",
        provider_request_id="provider-id",
    )


class _Query:
    def __init__(self):
        self.requests = []

    async def query(self, request):
        self.requests.append(request)
        return ProviderEvidence(
            provider=request.provider,
            event_id=f"query:{request.execution_id}",
            execution_id=request.execution_id,
            event_type="completed",
            occurred_at=datetime.now(timezone.utc),
            provider_resource_id=request.provider_resource_id,
        )


class _Journal:
    def __init__(self):
        self.commands = []

    async def apply_reconciliation(self, command):
        self.commands.append(command)
        return len(self.commands) + 10


def test_bounded_scanner_publishes_owner_command_and_owner_acknowledges(tmp_path):
    async def scenario():
        receipts = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await receipts.initialize()
        await receipts.accept(_receipt("execution"))
        authority = SQLiteReconciliationAuthority(receipts)
        await authority.bind_execution(
            execution_id="execution",
            generation_id="generation",
            provider="openai",
            owner_id="owner",
            strategy_id="provider-query-v1",
        )
        query = _Query()
        scanner = DaemonEvidenceReconciler(
            receipts=receipts,
            authority=authority,
            provider_query=query,
            concurrency=2,
            scan_limit=10,
            query_timeout_seconds=2,
        )
        assert await scanner.scan_once() == (1, 1)
        commands = await authority.read_owner_commands("owner")
        assert len(commands) == 1
        journal = _Journal()
        caller = CallerLogicalReconciler(
            owner_id="owner",
            generation_id="generation",
            strategy_id="provider-query-v1",
            commands=authority,
            journal=journal,
        )
        cursor = await caller.consume()
        return cursor, query, journal, await authority.get("execution")

    cursor, query, journal, record = asyncio.run(scenario())
    assert cursor == 1
    assert len(query.requests) == len(journal.commands) == 1
    assert record is not None
    assert record.state is ReconciliationState.OWNER_APPLIED
    assert record.acknowledgement is not None
    assert record.acknowledgement.owner_journal_revision == 11


def test_offline_owner_command_remains_durable_and_stale_strategy_is_rejected(tmp_path):
    async def scenario():
        receipts = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await receipts.initialize()
        await receipts.accept(_receipt("execution"))
        authority = SQLiteReconciliationAuthority(receipts)
        await authority.bind_execution(
            execution_id="execution",
            generation_id="generation",
            provider="openai",
            owner_id="owner",
            strategy_id="provider-query-v1",
        )
        scanner = DaemonEvidenceReconciler(
            receipts=receipts,
            authority=authority,
            provider_query=_Query(),
        )
        await scanner.scan_once()
        restarted = SQLiteReconciliationAuthority(SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3"))
        pending = await restarted.read_owner_commands("owner")
        caller = CallerLogicalReconciler(
            owner_id="owner",
            generation_id="generation",
            strategy_id="different-strategy",
            commands=restarted,
            journal=_Journal(),
        )
        await caller.consume()
        return pending, await restarted.get("execution")

    pending, record = asyncio.run(scenario())
    assert len(pending) == 1
    assert record is not None
    assert record.state is ReconciliationState.OWNER_REJECTED


def test_scanner_uses_durable_provider_binding_and_bounds_query_runtime(tmp_path):
    class SlowQuery:
        def __init__(self):
            self.requests = []

        async def query(self, request):
            self.requests.append(request)
            await asyncio.sleep(60)
            raise AssertionError("bounded provider query did not time out")

    async def scenario():
        receipts = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await receipts.initialize()
        receipt = _receipt("execution").model_copy(update={"operation": "batch.create"})
        await receipts.accept(receipt)
        authority = SQLiteReconciliationAuthority(receipts)
        await authority.bind_execution(
            execution_id="execution",
            generation_id="generation",
            provider="openai",
            owner_id="owner",
            strategy_id="provider-query-v1",
        )
        query = SlowQuery()
        scanner = DaemonEvidenceReconciler(
            receipts=receipts,
            authority=authority,
            provider_query=query,
            query_timeout_seconds=0.01,
        )
        outcome = await scanner.scan_once()
        return outcome, query.requests

    outcome, requests = asyncio.run(scenario())
    assert outcome == (1, 0)
    assert len(requests) == 1
    assert requests[0].provider == "openai"
