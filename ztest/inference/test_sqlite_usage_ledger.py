import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from mote.product.inference.backends.sqlite import ReceiptConflictError, SQLiteAttemptReceiptStore, SQLiteUsageLedger
from mote.runtime.clock import SystemClock


def test_usage_reserve_settle_release_are_idempotent_and_budgeted(tmp_path):
    async def scenario():
        authority = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority, clock_source=SystemClock())
        await ledger.configure_budget("tenant", "project", 100)
        first = await ledger.reserve(
            reservation_id="r1",
            attempt_id="a1",
            tenant_id="tenant",
            project_id="project",
            units=60,
            ttl_seconds=30,
        )
        assert (
            await ledger.reserve(
                reservation_id="r1",
                attempt_id="a1",
                tenant_id="tenant",
                project_id="project",
                units=60,
                ttl_seconds=30,
            )
            == first
        )
        with pytest.raises(ReceiptConflictError, match="exhausted"):
            await ledger.reserve(
                reservation_id="r2",
                attempt_id="a2",
                tenant_id="tenant",
                project_id="project",
                units=50,
                ttl_seconds=30,
            )
        settlement = await ledger.settle(first, settlement_id="s1", actual_units=40)
        assert await ledger.settle(first, settlement_id="s1", actual_units=40) == settlement
        second = await ledger.reserve(
            reservation_id="r2",
            attempt_id="a2",
            tenant_id="tenant",
            project_id="project",
            units=50,
            ttl_seconds=30,
        )
        released = await ledger.release(second, settlement_id="s2")
        assert released.actual_units == 0

    asyncio.run(scenario())


def test_usage_reconciliation_requires_higher_fence_and_expiry_reclaims_only_reserved(
    tmp_path,
):
    async def scenario():
        authority = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority, clock_source=SystemClock())
        await ledger.configure_budget("tenant", "project", 100)
        pending = await ledger.reserve(
            reservation_id="pending",
            attempt_id="a-pending",
            tenant_id="tenant",
            project_id="project",
            units=60,
            ttl_seconds=0.01,
        )
        await ledger.pending_reconciliation(pending, settlement_id="pending-marker")
        expired = await ledger.reserve(
            reservation_id="expired",
            attempt_id="a-expired",
            tenant_id="tenant",
            project_id="project",
            units=40,
            ttl_seconds=0.01,
        )
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        with pytest.raises(ValueError, match="higher fencing token"):
            await ledger.reconcile(
                pending,
                settlement_id="reconciled",
                actual_units=25,
                fencing_token=1,
            )
        reclaimed = await ledger.reclaim_expired(now=now, fencing_token=2)
        assert [item.reservation_id for item in reclaimed] == [expired.reservation_id]
        assert await ledger.reclaim_expired(now=now, fencing_token=2) == ()
        settled = await ledger.reconcile(
            pending,
            settlement_id="reconciled",
            actual_units=25,
            fencing_token=2,
        )
        assert settled.actual_units == 25
        assert (
            await ledger.reconcile(
                pending,
                settlement_id="reconciled",
                actual_units=25,
                fencing_token=2,
            )
            == settled
        )
        with pytest.raises(ReceiptConflictError):
            await ledger.reconcile(
                pending,
                settlement_id="different-id",
                actual_units=25,
                fencing_token=2,
            )

    asyncio.run(scenario())
