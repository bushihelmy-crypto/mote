import asyncio
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from mote.contracts.inference.execution_owner import (
    ExecutionEpochBinding,
    ExecutionId,
    ExecutionOwnerRecord,
    SharedExecutionVariant,
)
from mote.contracts.inference.identity import InferencePrincipal
from mote.contracts.inference.persisted_event import PersistedLifecycleEvent
from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.product.inference.backends.sqlite import (
    ReceiptConflictError,
    SQLiteAttemptReceiptStore,
    SQLiteBusyError,
    SQLiteIntegrityError,
)
from ztest.inference.test_generation import _artifact


def test_execution_owner_record_is_idempotent_conflict_safe_and_restart_readable(tmp_path):
    async def scenario():
        path = tmp_path / "authority.sqlite3"
        store = SQLiteAttemptReceiptStore(path)
        await store.initialize()
        record = ExecutionOwnerRecord(
            record_revision=1,
            execution_id=ExecutionId("execution-1"),
            variant=SharedExecutionVariant.FINITE,
            principal=InferencePrincipal(
                tenant_id="tenant",
                project_id="project",
                subject_id="subject",
                policy_revision="policy",
                delegation_digest="sha256:" + "a" * 64,
            ),
            application_scope="application",
            credential_scope="credential",
            generation_id="generation",
            generation_artifact_digest="sha256:" + "b" * 64,
            epoch=ExecutionEpochBinding(
                backup_epoch=0,
                admission_epoch=0,
                permit_trust_revision=1,
            ),
        )
        assert await store.put_owner_record(record) == record
        assert await store.put_owner_record(record) == record
        reopened = SQLiteAttemptReceiptStore(path)
        await reopened.initialize()
        assert await reopened.get_owner_record(ExecutionId("execution-1")) == record
        with pytest.raises(ReceiptConflictError):
            await reopened.put_owner_record(record.model_copy(update={"application_scope": "other"}))

    asyncio.run(scenario())


DIGEST = "sha256:" + "d" * 64


def _receipt(state, revision, *, fencing=1):
    committed = state not in {ReceiptState.ACCEPTED, ReceiptState.SEND_INTENT_DURABLE}
    return AttemptReceipt(
        attempt_id="a",
        generation_id="g",
        generation_artifact_digest=DIGEST,
        revision=revision,
        state=state,
        fencing_token=fencing,
        permit_digest=DIGEST if committed else None,
        permit_ordinal=1 if committed else None,
        request_digest=DIGEST,
        operation="chat.complete",
        idempotency_class="attempt",
        updated_at=datetime.now(timezone.utc),
    )


def test_sqlite_receipt_cas_and_outbox_are_atomic(tmp_path):
    async def scenario():
        store = SQLiteAttemptReceiptStore(tmp_path / "authority" / "gateway.sqlite3")
        await store.initialize()
        accepted = await store.accept(_receipt(ReceiptState.ACCEPTED, 1))
        assert await store.accept(accepted) == accepted
        intent = _receipt(ReceiptState.SEND_INTENT_DURABLE, 2)
        await store.compare_and_swap(intent, expected_revision=1, fencing_token=1)
        outbox = await store.read_outbox()
        assert [record.receipt_revision for record in outbox] == [1, 2]
        await store.mark_published(outbox[0].sequence)
        assert [record.receipt_revision for record in await store.read_outbox()] == [2]
        assert (tmp_path / "authority").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "authority" / "gateway.sqlite3").stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


def test_sqlite_receipt_rejects_revision_race_and_permit_reuse(tmp_path):
    async def scenario():
        store = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await store.initialize()
        await store.accept(_receipt(ReceiptState.ACCEPTED, 1))
        with pytest.raises(ReceiptConflictError, match="expected revision"):
            await store.compare_and_swap(
                _receipt(ReceiptState.SEND_INTENT_DURABLE, 2),
                expected_revision=9,
                fencing_token=1,
            )

    asyncio.run(scenario())


def test_sqlite_startup_check_and_verified_backup_restore(tmp_path):
    async def scenario():
        authority = tmp_path / "authority.sqlite3"
        backup = tmp_path / "backup" / "authority.sqlite3"
        store = SQLiteAttemptReceiptStore(authority)
        await store.initialize()
        generation = _artifact("generation-1")
        await store.stage_generation(generation)
        await store.activate_generation(generation.generation_id, generation.artifact_digest)
        await store.accept(_receipt(ReceiptState.ACCEPTED, 1))
        report = await store.verify_startup(hard_min_free_bytes=0)
        assert report.integrity == "ok"
        await store.backup_to(backup)
        await store.compare_and_swap(
            _receipt(ReceiptState.SEND_INTENT_DURABLE, 2),
            expected_revision=1,
            fencing_token=1,
        )
        await store.restore_from(backup)
        restored = await store.get("a", "g")
        assert restored is not None
        assert restored.state is ReceiptState.ACCEPTED
        assert backup.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


def test_sqlite_hard_watermark_and_corruption_evidence(tmp_path):
    async def scenario():
        store = SQLiteAttemptReceiptStore(tmp_path / "authority.sqlite3")
        await store.initialize()
        with pytest.raises(SQLiteIntegrityError, match="watermark"):
            await store.verify_startup(hard_min_free_bytes=2**63)
        evidence = await store.preserve_corrupt_copy()
        assert evidence.read_bytes() == (tmp_path / "authority.sqlite3").read_bytes()
        assert evidence.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


def test_sqlite_corruption_fails_readiness_and_preserves_exact_evidence(tmp_path):
    async def scenario():
        path = tmp_path / "authority.sqlite3"
        store = SQLiteAttemptReceiptStore(path)
        await store.initialize()
        corrupt = b"not-a-sqlite-database"
        path.with_name(path.name + "-wal").unlink(missing_ok=True)
        path.with_name(path.name + "-shm").unlink(missing_ok=True)
        path.write_bytes(corrupt)
        with pytest.raises(SQLiteIntegrityError, match="quick_check") as observed:
            await store.verify_startup(hard_min_free_bytes=0)
        assert observed.value is not None
        evidence = await store.preserve_corrupt_copy()
        assert evidence.read_bytes() == corrupt

    asyncio.run(scenario())


def test_sqlite_busy_wait_is_bounded_and_structured(tmp_path):
    async def scenario():
        path = tmp_path / "authority.sqlite3"
        store = SQLiteAttemptReceiptStore(path, busy_timeout_seconds=0.05)
        await store.initialize()
        lock = sqlite3.connect(path, isolation_level=None)
        lock.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        try:
            with pytest.raises(SQLiteBusyError, match="busy past deadline"):
                await store.accept(_receipt(ReceiptState.ACCEPTED, 1))
        finally:
            lock.rollback()
            lock.close()
        assert time.monotonic() - started < 1.0

    asyncio.run(scenario())


def test_sqlite_startup_reconciles_committed_execution_to_in_doubt(tmp_path):
    async def scenario():
        store = SQLiteAttemptReceiptStore(tmp_path / "authority.sqlite3")
        await store.initialize()
        await store.accept(_receipt(ReceiptState.ACCEPTED, 1))
        await store.compare_and_swap(
            _receipt(ReceiptState.SEND_INTENT_DURABLE, 2),
            expected_revision=1,
            fencing_token=1,
        )
        await store.compare_and_swap(
            _receipt(ReceiptState.SEND_COMMITTED, 3),
            expected_revision=2,
            fencing_token=1,
        )
        assert await store.reconcile_incomplete() == (1, 0)
        receipt = await store.get("a", "g")
        assert receipt is not None
        assert receipt.state is ReceiptState.IN_DOUBT
        assert receipt.revision == 4
        assert await store.reconcile_incomplete() == (0, 0)

    asyncio.run(scenario())


def test_sqlite_lifecycle_events_are_ordered_idempotent_and_restart_readable(tmp_path):
    async def scenario():
        path = tmp_path / "authority.sqlite3"
        store = SQLiteAttemptReceiptStore(path)
        await store.initialize()
        first = PersistedLifecycleEvent(
            execution_id="execution",
            sequence=1,
            receipt_revision=1,
            event_type="queued",
            payload=b"{}",
        )
        terminal = PersistedLifecycleEvent(
            execution_id="execution",
            sequence=2,
            receipt_revision=2,
            event_type="succeeded",
            payload=b'{"ok":true}',
            terminal=True,
        )
        assert await store.append_event(first) == first
        assert await store.append_event(first) == first
        await store.append_event(terminal)
        reopened = SQLiteAttemptReceiptStore(path)
        await reopened.initialize()
        assert await reopened.read_events("execution", after_sequence=1) == (terminal,)
        with pytest.raises(ReceiptConflictError, match="follows terminal"):
            await reopened.append_event(terminal.model_copy(update={"sequence": 3, "event_type": "impossible"}))

    asyncio.run(scenario())
