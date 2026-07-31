from __future__ import annotations

import asyncio

import pytest

from mote.orchestration.agents.lifecycle.spawn import SpawnPhase, SpawnRollbackError, SpawnTransaction


@pytest.mark.asyncio
async def test_rollback_is_reverse_order_and_idempotent():
    seen: list[str] = []
    transaction = SpawnTransaction()
    transaction.advance(SpawnPhase.ADMITTED)
    transaction.advance(SpawnPhase.RESIDENCY_RESERVED, lambda: seen.append("slot"))
    transaction.advance(SpawnPhase.IDENTITY_RESERVED, lambda: seen.append("identity"))

    await transaction.rollback()
    await transaction.rollback()

    assert seen == ["identity", "slot"]
    assert transaction.phase is SpawnPhase.ROLLED_BACK


@pytest.mark.asyncio
async def test_rollback_aggregates_failures_without_skipping_cleanup():
    seen: list[str] = []
    transaction = SpawnTransaction()
    transaction.advance(SpawnPhase.ADMITTED)

    def fail():
        seen.append("fail")
        raise RuntimeError("cleanup failed")

    transaction.advance(SpawnPhase.RESIDENCY_RESERVED, lambda: seen.append("slot"))
    transaction.advance(SpawnPhase.IDENTITY_RESERVED, fail)
    transaction.own(lambda: seen.append("route"))

    with pytest.raises(SpawnRollbackError) as error:
        await transaction.rollback()

    assert seen == ["route", "fail", "slot"]
    assert len(error.value.failures) == 1


@pytest.mark.asyncio
async def test_shielded_rollback_finishes_when_caller_is_cancelled():
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()
    transaction = SpawnTransaction()
    transaction.advance(SpawnPhase.ADMITTED)

    async def cleanup():
        started.set()
        await release.wait()
        cleaned.set()

    transaction.advance(SpawnPhase.RESIDENCY_RESERVED, cleanup)
    task = asyncio.create_task(transaction.rollback_shielded())
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert transaction.phase is SpawnPhase.ROLLED_BACK


def test_commit_requires_supervision_and_makes_transaction_terminal():
    transaction = SpawnTransaction()
    transaction.advance(SpawnPhase.ADMITTED)
    with pytest.raises(RuntimeError):
        transaction.commit()

    transaction.advance(SpawnPhase.SUPERVISED)
    transaction.commit()
    assert transaction.phase is SpawnPhase.COMMITTED
    with pytest.raises(RuntimeError):
        transaction.own(lambda: None)
