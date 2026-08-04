#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for CronScheduler — driven by a fake clock via direct _check() calls."""

from dataclasses import replace
from datetime import datetime

import pytest

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant, ClockIdentity, MonotonicMark
from mote.orchestration.automation import TriggerDisposition, TriggerReceipt
from mote.orchestration.automation.cron.lock import SchedulerLock
from mote.orchestration.automation.cron.scheduler import CronScheduler, is_recurring_task_aged
from mote.orchestration.automation.cron.store import CronTaskStore
from mote.orchestration.automation.cron.task import CronTask, CronTriggerIntent, DurableCronTaskId


def _ms(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second).timestamp() * 1000)


class Clock:
    def __init__(self, ms):
        self.ms = ms

    @property
    def durable_clock_identity(self) -> ClockIdentity:
        return UNIX_UTC_CLOCK

    def now(self) -> AbsoluteInstant:
        return AbsoluteInstant(1, UNIX_UTC_CLOCK, self.ms * 1_000_000)

    def monotonic_mark(self) -> MonotonicMark:
        return MonotonicMark("cron-scheduler-test", self.ms * 1_000_000)

    def set(self, ms):
        self.ms = ms


def make_sched(store, clock, **kwargs):
    """Build an owner scheduler and pre-load durable tasks (no asyncio loop)."""
    fired = []

    def accept(task, occurrence):
        fired.append(task)
        return TriggerReceipt(
            TriggerDisposition.ACCEPTED,
            receipt_id=occurrence.occurrence_id,
        )

    on_fire = kwargs.pop("on_fire", accept)
    lock = SchedulerLock("test-scheduler", base_dir=str(store.path.parent), clock_source=clock)
    fence = lock.acquire()
    assert fence is not None
    sched = CronScheduler(store, on_fire=on_fire, clock_source=clock, lock=lock, **kwargs)
    sched._is_owner = True
    sched._fence = fence
    sched._reload_durable(force=True)
    return sched, fired


def test_constructor_initializes_complete_lifecycle_state(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    clock = Clock(_ms(2026, 6, 15))
    scheduler = CronScheduler(
        store,
        on_fire=lambda task, occurrence: TriggerReceipt(
            TriggerDisposition.ACCEPTED,
            receipt_id=occurrence.occurrence_id,
        ),
        clock_source=clock,
    )

    assert scheduler._durable == []
    assert scheduler._last_revision == -1
    assert scheduler._is_owner is False
    assert scheduler._fence is None
    assert scheduler._runner is not None


def test_cron_trigger_schema_rejects_boolean_version() -> None:
    with pytest.raises(ValueError, match="schema version"):
        CronTriggerIntent(
            schema_version=True,  # type: ignore[arg-type]
            task_id=DurableCronTaskId("00000000000000000000000000000000"),
            task_revision=1,
            target="agent",
            content="run",
            scheduled_at_ms=1,
            fired_at_ms=1,
            attempt=1,
        )


def _recurring_task(created_at):
    return CronTask(
        id="00000000000000000000000000000000",  # deterministic zero-valued identity
        cron="* * * * *",
        prompt="ping",
        created_at=created_at,
        recurring=True,
        durable=True,
        timezone_name="Asia/Shanghai",
    )


def test_recurring_fires_and_backfills_last_fired_at(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    created = _ms(2026, 6, 15, 10, 0, 30)
    store.add(_recurring_task(created), capacity_limit=50)

    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock)
    sched._check()

    assert len(fired) == 1
    assert fired[0].prompt == "ping"
    # Rescheduled forward from now.
    assert sched.next_fire_time() == _ms(2026, 6, 15, 10, 2, 0)
    # last_fired_at persisted to disk.
    reloaded = store.load()
    assert reloaded[0].last_fired_at == _ms(2026, 6, 15, 10, 1, 0)


def test_recurring_does_not_fire_before_due(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)), capacity_limit=50)
    clock = Clock(_ms(2026, 6, 15, 10, 0, 45))  # before 10:01:00
    sched, fired = make_sched(store, clock)
    sched._check()
    assert fired == []


def test_one_shot_fires_then_deleted(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    task = CronTask(
        id="00000000000000000000000000000000",
        cron="0 12 * * *",
        prompt="lunch",
        created_at=_ms(2026, 6, 15, 10, 0, 0),
        recurring=False,
        durable=True,
        timezone_name="Asia/Shanghai",
    )
    store.add(task, capacity_limit=50)
    clock = Clock(_ms(2026, 6, 15, 12, 0, 0))
    sched, fired = make_sched(store, clock)
    sched._check()

    assert len(fired) == 1
    assert store.list() == []
    assert sched.next_fire_time() is None


def test_recurring_expiry_recycle(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    # Created 8 days ago → exceeds 7-day default max age.
    store.add(_recurring_task(_ms(2026, 6, 7, 10, 0, 0)), capacity_limit=50)
    clock = Clock(_ms(2026, 6, 15, 10, 0, 0))
    sched, fired = make_sched(store, clock)
    sched._check()

    assert len(fired) == 1  # fires one last time
    assert store.list() == []  # then removed


def test_is_recurring_task_aged():
    base = _ms(2026, 6, 1)
    t = _recurring_task(base)
    week = 7 * 24 * 60 * 60 * 1000
    assert is_recurring_task_aged(t, base + week, week) is True
    assert is_recurring_task_aged(t, base + week - 1, week) is False
    assert is_recurring_task_aged(t, base + week, 0) is False  # 0 = unlimited
    t = replace(t, permanent=True)
    assert is_recurring_task_aged(t, base + week, week) is False


def test_missed_compensation(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    task = CronTask(
        id="00000000000000000000000000000000",
        cron="0 9 * * *",
        prompt="morning",
        created_at=_ms(2026, 6, 14, 8, 0, 0),  # next-from-created = 6/14 9:00 (past)
        recurring=False,
        durable=True,
        timezone_name="Asia/Shanghai",
    )
    store.add(task, capacity_limit=50)
    clock = Clock(_ms(2026, 6, 15, 10, 0, 0))
    missed_seen = []
    sched, fired = make_sched(store, clock, on_missed=missed_seen.append)
    sched._run_missed()

    assert len(missed_seen) == 1
    assert missed_seen[0][0].prompt == "morning"
    assert fired == []  # on_missed provided → on_fire not used
    # Observation alone cannot advance/delete the durable occurrence.
    assert [task.id for task in store.list()] == [task.id]


def test_idle_gate_defers_fire(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)), capacity_limit=50)
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock, is_idle=lambda: False)
    sched._check()
    assert fired == []


def test_killed_gate_skips(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)), capacity_limit=50)
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock, is_killed=lambda: True)
    sched._check()
    assert fired == []


def test_revision_reconcile_reload(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    # No file yet → empty load.
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock)
    assert sched._durable == []

    # A canonical command advances the revision; the next tick adopts it.
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)), capacity_limit=50)
    sched._check()
    assert len(fired) == 1


def test_session_only_fires_without_owner(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    task = CronTask(
        id="00000000000000000000000000000000",
        cron="* * * * *",
        prompt="mem",
        created_at=_ms(2026, 6, 15, 10, 0, 30),
        recurring=True,
        durable=False,
        timezone_name="Asia/Shanghai",
    )
    store.add(task, capacity_limit=50)
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock)
    sched._is_owner = False  # not lock owner — session tasks still fire
    sched._check()
    assert len(fired) == 1


def test_task_filter_excludes(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)), capacity_limit=50)
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock, task_filter=lambda t: False)
    sched._check()
    assert fired == []
    assert sched.next_fire_time() is None
