from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mote.contracts.conversation import MessageQueue
from mote.contracts.task.lifecycle import (
    BackgroundTaskAdmissionClosed,
    BackgroundTaskDrainDisposition,
    BackgroundTaskOwner,
    BackgroundTaskPoolState,
)
from mote.orchestration.background_tasks.pool import BackgroundTaskPool
from mote.orchestration.background_tasks.results.store import TaskOutputStore


async def _blocked(gate: asyncio.Event) -> str:
    await gate.wait()
    return "done"


def _owner(suffix: str) -> BackgroundTaskOwner:
    return BackgroundTaskOwner("process-1", f"agent-{suffix}", f"incarnation-{suffix}")


@pytest.mark.asyncio
async def test_close_admission_is_atomic_with_pin_acquisition() -> None:
    owner = _owner("one")
    pool = BackgroundTaskPool(MessageQueue(), owner=owner)
    gate = asyncio.Event()
    acceptance = pool.submit(lambda: _blocked(gate), "blocked", timeout=None)

    snapshot = pool.close_admission(owner=owner)
    assert snapshot.state is BackgroundTaskPoolState.DRAINING
    assert snapshot.references == (acceptance.reference,)
    with pytest.raises(BackgroundTaskAdmissionClosed):
        pool.submit(lambda: _blocked(gate), "late", timeout=None)

    receipt = await pool.drain(owner=owner, timeout_seconds=1.0)
    assert receipt.disposition is BackgroundTaskDrainDisposition.SETTLED
    assert pool.pin_snapshot(owner=owner).state is BackgroundTaskPoolState.CLOSED


@pytest.mark.asyncio
async def test_stale_owner_cannot_close_or_drain_another_incarnation() -> None:
    owner = _owner("current")
    stale = _owner("stale")
    pool = BackgroundTaskPool(MessageQueue(), owner=owner)
    with pytest.raises(RuntimeError, match="owner/incarnation lost"):
        pool.close_admission(owner=stale)
    receipt = await pool.drain(owner=stale, timeout_seconds=0.1)
    assert receipt.disposition is BackgroundTaskDrainDisposition.OWNER_LOST
    assert pool.pin_snapshot(owner=owner).state is BackgroundTaskPoolState.ACTIVE
    await pool.aclose()


class _FailingOutputStore(TaskOutputStore):
    async def flush(self, task_id: str) -> None:
        raise OSError("flush failed")


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_draining_and_pin(tmp_path: Path) -> None:
    owner = _owner("failure")
    store = _FailingOutputStore(base_dir=tmp_path, session_id="failure")
    pool = BackgroundTaskPool(MessageQueue(), owner=owner, output_store=store)
    pool.submit(lambda: asyncio.sleep(0, result="done"), "output", progress=True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    receipt = await pool.drain(owner=owner, timeout_seconds=1.0)
    assert receipt.disposition is BackgroundTaskDrainDisposition.CLEANUP_FAILED
    assert receipt.remaining
    snapshot = pool.pin_snapshot(owner=owner)
    assert snapshot.state is BackgroundTaskPoolState.DRAINING
    assert snapshot.pin_count == 1


@pytest.mark.asyncio
async def test_non_cooperative_work_returns_bounded_timeout_and_stays_pinned() -> None:
    owner = _owner("non-cooperative")
    pool = BackgroundTaskPool(MessageQueue(), owner=owner)
    release = asyncio.Event()

    async def resist_cancellation() -> str:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return "released"

    pool.submit(resist_cancellation, "resist", timeout=None)
    await asyncio.sleep(0)
    receipt = await pool.drain(owner=owner, timeout_seconds=0.01)
    assert receipt.disposition is BackgroundTaskDrainDisposition.DRAINING_TIMEOUT
    assert receipt.remaining
    assert pool.pin_snapshot(owner=owner).state is BackgroundTaskPoolState.DRAINING

    release.set()
    receipt = await pool.drain(owner=owner, timeout_seconds=2.0)
    assert receipt.disposition is BackgroundTaskDrainDisposition.SETTLED


class _FailingInbox(MessageQueue):
    def push(self, msg, priority=0) -> None:
        raise OSError("inbox unavailable")


@pytest.mark.asyncio
async def test_terminal_notification_failure_keeps_pin_for_typed_cleanup_failure() -> None:
    owner = _owner("notification")
    pool = BackgroundTaskPool(_FailingInbox(), owner=owner)
    pool.submit(lambda: asyncio.sleep(0, result="done"), "notify")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    receipt = await pool.drain(owner=owner, timeout_seconds=1.0)
    assert receipt.disposition is BackgroundTaskDrainDisposition.CLEANUP_FAILED
    assert receipt.remaining
    assert "notification delivery failed" in (receipt.failure or "")


@pytest.mark.asyncio
async def test_one_pool_drain_does_not_affect_another_pool() -> None:
    first = BackgroundTaskPool(MessageQueue(), owner=_owner("first"))
    second = BackgroundTaskPool(MessageQueue(), owner=_owner("second"))
    await first.aclose()
    task_id = second.submit(lambda: asyncio.sleep(0, result="ok"), "second")
    await second.wait_all()
    assert second.get_task_info(task_id).result == "ok"
    await second.aclose()


def test_residency_does_not_reach_into_pool_private_state() -> None:
    residency = Path("orchestration/agents/residency")
    source = "\n".join(path.read_text(encoding="utf-8") for path in residency.rglob("*.py"))
    for private_name in ("._tasks", "._meta", "._operations", "._work_pins"):
        assert private_name not in source


def test_role_and_residency_consume_typed_lifecycle_surface() -> None:
    role_source = Path("runtime/agent/role.py").read_text(encoding="utf-8")
    residency_source = Path("orchestration/agents/residency/manager.py").read_text(encoding="utf-8")
    port_source = Path("contracts/ports/task/operations.py").read_text(encoding="utf-8")

    assert "bg_pool.close_admission(owner=bg_pool.owner)" in role_source
    assert "receipt = await bg_pool.drain(" in role_source
    assert "pin_snapshot = await runtime.role.prepare_for_eviction()" in residency_source
    assert residency_source.index("prepare_for_eviction()") < residency_source.index("_store.materialize(")
    assert "def close_admission(" in port_source
    assert "async def drain(" in port_source
    for private_name in ("_tasks", "_meta", "_operations", "_work_pins"):
        assert private_name not in port_source
