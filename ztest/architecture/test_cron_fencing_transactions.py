from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mote.orchestration.automation import TriggerDisposition, TriggerReceipt
from mote.orchestration.automation.cron.lock import SchedulerFence, SchedulerFenceLost, SchedulerLock
from mote.orchestration.automation.cron.store import CronTaskStore
from mote.orchestration.automation.cron.task import CronTask
from mote.runtime.clock import SystemClock


def _task(prompt: str = "ping") -> CronTask:
    return CronTask.new("* * * * *", prompt, 1_000)


def _lease(base_dir: Path, owner: str) -> tuple[SchedulerLock, SchedulerFence]:
    lock = SchedulerLock(owner, base_dir=str(base_dir), clock_source=SystemClock())
    fence = lock.acquire()
    assert fence is not None
    return lock, fence


def test_reacquire_advances_epoch_and_old_fence_cannot_claim(tmp_path) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    task = store.add(_task(), capacity_limit=50)
    first, stale = _lease(store.path.parent, "first")
    first.release()
    second, current = _lease(store.path.parent, "second")

    assert current.epoch > stale.epoch
    with pytest.raises(SchedulerFenceLost):
        store.claim_occurrence(
            fence=stale,
            task_id=str(task.id),
            expected_task_revision=task.revision,
            scheduled_at_ms=2_000,
            observed_at_ms=2_000,
            delete_on_accept=False,
        )
    assert store.load_snapshot().occurrences == ()
    second.release()


def test_stale_fence_cannot_begin_or_settle_dispatch(tmp_path) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    task = store.add(_task(), capacity_limit=50)
    first, stale = _lease(store.path.parent, "first")
    occurrence = store.claim_occurrence(
        fence=stale,
        task_id=str(task.id),
        expected_task_revision=task.revision,
        scheduled_at_ms=2_000,
        observed_at_ms=2_000,
        delete_on_accept=False,
    )
    dispatch = store.begin_dispatch(
        occurrence.occurrence_id,
        fence=stale,
        now_ms=2_000,
    )
    first.release()
    second, _current = _lease(store.path.parent, "second")

    with pytest.raises(SchedulerFenceLost):
        store.begin_dispatch(occurrence.occurrence_id, fence=stale, now_ms=2_001)
    with pytest.raises(SchedulerFenceLost):
        store.settle_receipt(
            occurrence.occurrence_id,
            fence=stale,
            expected_attempt=dispatch.attempt,
            receipt=TriggerReceipt(TriggerDisposition.ACCEPTED, receipt_id="receipt"),
            settled_at_ms=2_001,
        )
    with pytest.raises(SchedulerFenceLost):
        store.mark_in_doubt(
            occurrence.occurrence_id,
            fence=stale,
            expected_attempt=dispatch.attempt,
            reason="stale owner",
        )
    second.release()


def test_capacity_check_and_commit_are_one_store_command(tmp_path) -> None:
    first = CronTaskStore(base_dir=str(tmp_path))
    second = CronTaskStore(base_dir=str(tmp_path))

    def create(store: CronTaskStore, prompt: str) -> str:
        return str(store.add(_task(prompt), capacity_limit=1).id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(create, first, "first"),
            executor.submit(create, second, "second"),
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ValueError:
                outcomes.append("capacity-rejected")

    assert outcomes.count("capacity-rejected") == 1
    assert len(first.load()) == 1


def test_product_cli_uses_command_service_without_store_mutation_bypass() -> None:
    source = Path("product/entrypoints/cron/cli.py").read_text(encoding="utf-8")

    assert "CronTaskCommands" in source
    assert "CronTask.new" not in source
    assert "store.add(" not in source
    assert "store.remove(" not in source
