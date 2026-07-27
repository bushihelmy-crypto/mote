#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for CronScheduler — driven by a fake clock via direct _check() calls."""

from datetime import datetime

from mote.orchestration.environment.scheduling.scheduler import CronScheduler, is_recurring_task_aged
from mote.orchestration.environment.scheduling.store import CronTaskStore
from mote.orchestration.environment.scheduling.task import CronTask


def _ms(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second).timestamp() * 1000)


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return float(self.ms)

    def set(self, ms):
        self.ms = ms


def make_sched(store, clock, **kwargs):
    """Build an owner scheduler and pre-load durable tasks (no asyncio loop)."""
    fired = []
    on_fire = kwargs.pop("on_fire", fired.append)
    sched = CronScheduler(store, on_fire=on_fire, clock=clock, **kwargs)
    sched._is_owner = True
    sched._reload_durable(force=True)
    return sched, fired


def _recurring_task(created_at):
    return CronTask(
        id="00000000",  # hashes to 0 → deterministic, zero jitter
        cron="* * * * *",
        prompt="ping",
        created_at=created_at,
        recurring=True,
        durable=True,
    )


def test_recurring_fires_and_backfills_last_fired_at(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    created = _ms(2026, 6, 15, 10, 0, 30)
    store.add(_recurring_task(created))

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
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)))
    clock = Clock(_ms(2026, 6, 15, 10, 0, 45))  # before 10:01:00
    sched, fired = make_sched(store, clock)
    sched._check()
    assert fired == []


def test_one_shot_fires_then_deleted(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    task = CronTask(
        id="00000000",
        cron="0 12 * * *",
        prompt="lunch",
        created_at=_ms(2026, 6, 15, 10, 0, 0),
        recurring=False,
        durable=True,
    )
    store.add(task)
    clock = Clock(_ms(2026, 6, 15, 12, 0, 0))
    sched, fired = make_sched(store, clock)
    sched._check()

    assert len(fired) == 1
    assert store.list() == []
    assert sched.next_fire_time() is None


def test_recurring_expiry_recycle(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    # Created 8 days ago → exceeds 7-day default max age.
    store.add(_recurring_task(_ms(2026, 6, 7, 10, 0, 0)))
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
    t.permanent = True
    assert is_recurring_task_aged(t, base + week, week) is False


def test_missed_compensation(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    task = CronTask(
        id="00000000",
        cron="0 9 * * *",
        prompt="morning",
        created_at=_ms(2026, 6, 14, 8, 0, 0),  # next-from-created = 6/14 9:00 (past)
        recurring=False,
        durable=True,
    )
    store.add(task)
    clock = Clock(_ms(2026, 6, 15, 10, 0, 0))
    missed_seen = []
    sched, fired = make_sched(store, clock, on_missed=missed_seen.append)
    sched._run_missed()

    assert len(missed_seen) == 1
    assert missed_seen[0][0].prompt == "morning"
    assert fired == []  # on_missed provided → on_fire not used
    assert store.list() == []  # missed one-shots removed


def test_idle_gate_defers_fire(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)))
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock, is_idle=lambda: False)
    sched._check()
    assert fired == []


def test_killed_gate_skips(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)))
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock, is_killed=lambda: True)
    sched._check()
    assert fired == []


def test_mtime_hot_reload(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    # No file yet → empty load.
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock)
    assert sched._durable == []

    # External write appears; the next tick detects the mtime change and reloads.
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)))
    sched._check()
    assert len(fired) == 1


def test_session_only_fires_without_owner(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    task = CronTask(
        id="00000000",
        cron="* * * * *",
        prompt="mem",
        created_at=_ms(2026, 6, 15, 10, 0, 30),
        recurring=True,
        durable=False,
    )
    store.add(task)
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock)
    sched._is_owner = False  # not lock owner — session tasks still fire
    sched._check()
    assert len(fired) == 1


def test_task_filter_excludes(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    store.add(_recurring_task(_ms(2026, 6, 15, 10, 0, 30)))
    clock = Clock(_ms(2026, 6, 15, 10, 1, 0))
    sched, fired = make_sched(store, clock, task_filter=lambda t: False)
    sched._check()
    assert fired == []
    assert sched.next_fire_time() is None
