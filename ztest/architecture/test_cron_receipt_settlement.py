from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant, MonotonicMark
from mote.orchestration.automation import TriggerDisposition, TriggerReceipt
from mote.orchestration.automation.cron.expression import _next_cron_run_ms
from mote.orchestration.automation.cron.lock import SchedulerFence, SchedulerLock
from mote.orchestration.automation.cron.scheduler import CronScheduler
from mote.orchestration.automation.cron.store import (
    CronOccurrenceState,
    CronRevisionConflict,
    CronStoreCorruptionError,
    CronTaskStore,
)
from mote.orchestration.automation.cron.task import CronTask
from mote.runtime.clock import SystemClock


def _ms(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 6, 15, hour, minute, second).timestamp() * 1000)


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    @property
    def durable_clock_identity(self):
        return UNIX_UTC_CLOCK

    def now(self) -> AbsoluteInstant:
        return AbsoluteInstant(1, UNIX_UTC_CLOCK, self.value * 1_000_000)

    def monotonic_mark(self) -> MonotonicMark:
        return MonotonicMark("cron-receipt-test", self.value * 1_000_000)


def _task(*, recurring: bool = True) -> CronTask:
    return CronTask(
        id="00000000000000000000000000000000",
        cron="* * * * *",
        prompt="ping",
        created_at=_ms(10, 0, 30),
        recurring=recurring,
        durable=True,
        timezone_name="Asia/Shanghai",
    )


def _scheduler(store, clock, dispatch):
    fence, lock = _lease(store)
    scheduler = CronScheduler(store, dispatch, clock_source=clock, lock=lock)
    scheduler._is_owner = True
    scheduler._fence = fence
    scheduler._reload_durable(force=True)
    return scheduler


def _lease(store: CronTaskStore) -> tuple[SchedulerFence, SchedulerLock]:
    prior = getattr(store, "_test_scheduler_lock", None)
    if prior is not None and prior.is_held:
        prior.release()
    lock = SchedulerLock(
        "receipt-test",
        base_dir=str(store.path.parent),
        clock_source=SystemClock(),
    )
    fence = lock.acquire()
    assert fence is not None
    store._test_scheduler_lock = lock
    return fence, lock


def test_accepted_receipt_is_the_only_path_that_advances_recurring_task(tmp_path) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_task(), capacity_limit=50)
    seen = []

    def dispatch(task, occurrence):
        snapshot = store.load_snapshot()
        assert snapshot.occurrences[0].state is CronOccurrenceState.DISPATCHING
        seen.append(occurrence.occurrence_id)
        return TriggerReceipt(
            TriggerDisposition.ACCEPTED,
            receipt_id=occurrence.occurrence_id,
        )

    _scheduler(store, Clock(_ms(10, 1)), dispatch)._check()

    snapshot = store.load_snapshot()
    assert snapshot.tasks[0].revision == 1
    assert snapshot.tasks[0].last_fired_at == _ms(10, 1)
    assert snapshot.occurrences[0].state is CronOccurrenceState.ACCEPTED
    assert snapshot.occurrences[0].receipt_id == seen[0]


def test_deferred_retries_same_logical_trigger_with_bounded_backoff(tmp_path) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_task(recurring=False), capacity_limit=50)
    clock = Clock(_ms(10, 1))
    seen = []

    def dispatch(_task, occurrence):
        seen.append((occurrence.occurrence_id, occurrence.attempt))
        if len(seen) == 1:
            return TriggerReceipt(TriggerDisposition.DEFERRED, reason="target active")
        return TriggerReceipt(
            TriggerDisposition.ACCEPTED,
            receipt_id=occurrence.occurrence_id,
        )

    scheduler = _scheduler(store, clock, dispatch)
    scheduler._check()
    assert len(store.load()) == 1
    deferred = store.load_snapshot().occurrences[0]
    assert deferred.state is CronOccurrenceState.DEFERRED
    assert deferred.next_attempt_at_ms == _ms(10, 1, 1)

    clock.value = _ms(10, 1, 1)
    scheduler._check()
    assert store.load() == []
    assert seen == [(seen[0][0], 1), (seen[0][0], 2)]


@pytest.mark.parametrize(
    ("receipt", "expected_state"),
    [
        (
            TriggerReceipt(TriggerDisposition.REJECTED, reason="missing target"),
            CronOccurrenceState.REJECTED,
        ),
    ],
)
def test_rejected_does_not_advance_or_delete(tmp_path, receipt, expected_state) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_task(recurring=False), capacity_limit=50)
    calls = []

    def dispatch(_task, occurrence):
        calls.append(occurrence.occurrence_id)
        return receipt

    _scheduler(store, Clock(_ms(10, 1)), dispatch)._check()
    assert len(store.load()) == 1
    assert store.load_snapshot().occurrences[0].state is expected_state

    _scheduler(store, Clock(_ms(10, 2)), dispatch)._check()
    assert len(calls) == 1


def test_dispatch_exception_is_in_doubt_and_is_not_automatically_replayed(tmp_path) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_task(recurring=False), capacity_limit=50)
    calls = []

    def dispatch(_task, occurrence):
        calls.append(occurrence.occurrence_id)
        raise TimeoutError("unknown external outcome")

    _scheduler(store, Clock(_ms(10, 1)), dispatch)._check()
    assert store.load_snapshot().occurrences[0].state is CronOccurrenceState.IN_DOUBT
    assert len(store.load()) == 1

    _scheduler(store, Clock(_ms(10, 2)), dispatch)._check()
    assert len(calls) == 1


def test_restart_after_dispatch_begin_marks_in_doubt_without_calling_sink(tmp_path) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    task = store.add(_task(recurring=False), capacity_limit=50)
    fence, _lock = _lease(store)
    occurrence = store.claim_occurrence(
        fence=fence,
        task_id=str(task.id),
        expected_task_revision=0,
        scheduled_at_ms=_ms(10, 1),
        observed_at_ms=_ms(10, 1),
        delete_on_accept=True,
    )
    store.begin_dispatch(occurrence.occurrence_id, fence=fence, now_ms=_ms(10, 1))
    calls = []

    scheduler = _scheduler(
        store,
        Clock(_ms(10, 2)),
        lambda *_args: calls.append(True),
    )
    scheduler._check()

    assert calls == []
    assert store.load_snapshot().occurrences[0].state is CronOccurrenceState.IN_DOUBT


def test_mismatched_receipt_and_delete_race_fail_closed(tmp_path) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    task = store.add(_task(recurring=False), capacity_limit=50)
    fence, _lock = _lease(store)
    occurrence = store.claim_occurrence(
        fence=fence,
        task_id=str(task.id),
        expected_task_revision=0,
        scheduled_at_ms=_ms(10, 1),
        observed_at_ms=_ms(10, 1),
        delete_on_accept=True,
    )
    dispatch = store.begin_dispatch(occurrence.occurrence_id, fence=fence, now_ms=_ms(10, 1))

    with pytest.raises(CronRevisionConflict, match="attempt"):
        store.settle_receipt(
            occurrence.occurrence_id,
            fence=fence,
            expected_attempt=dispatch.attempt + 1,
            receipt=TriggerReceipt(TriggerDisposition.ACCEPTED, receipt_id="wrong"),
            settled_at_ms=_ms(10, 1),
        )
    with pytest.raises(CronRevisionConflict, match="unsettled"):
        store.remove([str(task.id)])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(state="unknown"),
        lambda item: item.update(attempt=True),
        lambda item: item.update(extra="field"),
        lambda item: item.update(occurrence_id="wrong"),
    ],
)
def test_occurrence_codec_is_strict(tmp_path, mutation) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    task = store.add(_task(), capacity_limit=50)
    fence, _lock = _lease(store)
    store.claim_occurrence(
        fence=fence,
        task_id=str(task.id),
        expected_task_revision=0,
        scheduled_at_ms=_ms(10, 1),
        observed_at_ms=_ms(10, 1),
        delete_on_accept=False,
    )
    body = json.loads(store.path.read_text(encoding="utf-8"))
    mutation(body["occurrences"][0])
    store.path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(CronStoreCorruptionError):
        store.load_snapshot()


def test_dst_fold_uses_earliest_occurrence_and_gap_is_skipped() -> None:
    zone = ZoneInfo("America/New_York")
    before_fold = int(datetime(2026, 11, 1, 0, 0, tzinfo=zone).timestamp() * 1000)
    first_fold = _next_cron_run_ms("30 1 * * *", before_fold, timezone_name="America/New_York")
    assert first_fold == int(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc).timestamp() * 1000)
    assert _next_cron_run_ms("30 1 * * *", first_fold, timezone_name="America/New_York") == int(
        datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc).timestamp() * 1000
    )

    before_gap = int(datetime(2026, 3, 8, 0, 0, tzinfo=zone).timestamp() * 1000)
    assert _next_cron_run_ms("30 2 * * *", before_gap, timezone_name="America/New_York") == int(
        datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc).timestamp() * 1000
    )


def test_temporal_policies_are_typed_and_durable(tmp_path) -> None:
    store = CronTaskStore(base_dir=str(tmp_path))
    task = store.add(
        CronTask.new(
            "30 1 * * *",
            "ping",
            1_000,
            timezone_name="America/New_York",
        ),
        capacity_limit=50,
    )

    restored = store.load()[0]
    assert restored.timezone_name == "America/New_York"
    assert restored.misfire_policy.value == "fire_once"
    assert restored.overlap_policy.value == "forbid"
    assert restored.dst_policy.value == "earliest_fold_skip_gap"
    assert restored.to_dict() == task.to_dict()
