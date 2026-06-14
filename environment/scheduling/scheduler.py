#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CronScheduler — the polling scheduling engine (port of cronScheduler.ts).

A pure asyncio scheduler that ticks every ``check_interval`` seconds and fires due
tasks via an injected ``on_fire(task)`` callback. It deliberately knows nothing
about :class:`AgentControl` — the glue layer (:mod:`service`) wires the callback
to message delivery, keeping the engine free of an ``environment -> control``
import cycle.

Key behaviors carried over from upstream:

  * **disk is truth** — durable next-fire times are recomputed from the stored
    ``last_fired_at``/``created_at`` so a restart reconstructs the same schedule,
  * **single writer** — durable tasks fire only while the optional :class:`SchedulerLock`
    is held, so two sessions in one workspace never double-fire,
  * **hot reload** — the store's file ``mtime`` is polled each tick; an external
    edit triggers a reload (the dependency-free equivalent of a file watcher),
  * **idle gating** — firing is deferred while the target agent is mid-turn,
  * **missed compensation** — one-shot tasks whose window passed while the process
    was down are surfaced once at startup,
  * **jitter + expiry** — deterministic per-task spread and recurring auto-expiry.

The clock is injectable (``clock() -> epoch ms``) so tests drive time directly.
"""

from __future__ import annotations

import asyncio
import math
from typing import Callable, Dict, List, Optional

from metagpt.common.logs import log_class
from metagpt.environment.scheduling.cron import (
    _next_cron_run_ms,
    jittered_next_cron_run_ms,
    one_shot_jittered_next_cron_run_ms,
)
from metagpt.environment.scheduling.lock import SchedulerLock
from metagpt.environment.scheduling.store import CronTaskStore
from metagpt.environment.scheduling.task import (
    DEFAULT_CRON_JITTER_CONFIG,
    CronJitterConfig,
    CronTask,
)


def _default_clock() -> float:
    import time

    return time.time() * 1000.0


def is_recurring_task_aged(task: CronTask, now_ms: int, max_age_ms: int) -> bool:
    """True when a non-permanent recurring task is older than ``max_age_ms``.

    ``max_age_ms == 0`` means unlimited (never ages out). Extracted for
    testability, mirroring the upstream ``isRecurringTaskAged``.
    """
    if max_age_ms == 0:
        return False
    return bool(task.recurring and not task.permanent and now_ms - task.created_at >= max_age_ms)


@log_class(level="DEBUG", exclude={"next_fire_time"})
class CronScheduler:
    """Ticks every ``check_interval`` and fires due tasks via ``on_fire``."""

    def __init__(
        self,
        store: CronTaskStore,
        on_fire: Callable[[CronTask], None],
        *,
        is_idle: Optional[Callable[[], bool]] = None,
        on_missed: Optional[Callable[[List[CronTask]], None]] = None,
        is_killed: Optional[Callable[[], bool]] = None,
        task_filter: Optional[Callable[[CronTask], bool]] = None,
        lock: Optional[SchedulerLock] = None,
        jitter_config: Optional[Callable[[], CronJitterConfig]] = None,
        clock: Optional[Callable[[], float]] = None,
        check_interval: float = 1.0,
    ):
        self._store = store
        self._on_fire = on_fire
        self._is_idle = is_idle or (lambda: True)
        self._on_missed = on_missed
        self._is_killed = is_killed or (lambda: False)
        self._task_filter = task_filter
        self._lock = lock
        self._jitter_config = jitter_config
        self._clock = clock or _default_clock
        self._check_interval = check_interval

        # Per-task next-fire times (epoch ms; ``math.inf`` == "never").
        self._next_fire_at: Dict[str, float] = {}
        # Ids already surfaced as missed — avoids re-asking before removal lands.
        self._missed_asked: set[str] = set()
        # Cached durable tasks + the mtime they were loaded at (hot-reload anchor).
        self._durable: List[CronTask] = []
        self._last_mtime: Optional[float] = None
        self._is_owner = False
        self._stopped = True
        self._loop_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Acquire the lock (if any), run missed compensation, and start the loop."""
        self._stopped = False
        self._is_owner = self._lock.acquire() if self._lock is not None else True
        self._reload_durable(force=True)
        self._run_missed()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the loop and release the lock."""
        self._stopped = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        if self._lock is not None and self._is_owner:
            self._is_owner = False
            self._lock.release()

    def next_fire_time(self) -> Optional[int]:
        """Soonest scheduled fire (epoch ms) across loaded tasks, or ``None``."""
        soonest = math.inf
        for value in self._next_fire_at.values():
            if value < soonest:
                soonest = value
        return None if soonest == math.inf else int(soonest)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stopped:
            try:
                self._check()
            except Exception:  # noqa: BLE001 — best-effort tick; keep scheduling
                pass
            await asyncio.sleep(self._check_interval)

    def _get_cfg(self) -> CronJitterConfig:
        return self._jitter_config() if self._jitter_config is not None else DEFAULT_CRON_JITTER_CONFIG

    def _reload_durable(self, *, force: bool = False) -> None:
        """Reload durable tasks from disk when the file mtime changed."""
        mtime = self._store.mtime()
        if force or mtime != self._last_mtime:
            self._last_mtime = mtime
            self._durable = self._store.load()

    # ------------------------------------------------------------------
    # Core tick
    # ------------------------------------------------------------------
    def _check(self) -> None:
        if self._is_killed():
            return
        # Defer while the target agent is mid-turn (don't interrupt a running turn).
        if not self._is_idle():
            return

        now = int(self._clock())
        cfg = self._get_cfg()
        self._reload_durable()

        seen: set[str] = set()
        fired_durable_recurring: List[str] = []

        def process(task: CronTask, is_session: bool) -> None:
            if self._task_filter is not None and not self._task_filter(task):
                return
            seen.add(task.id)

            next_fire = self._next_fire_at.get(task.id)
            if next_fire is None:
                # First sight — anchor recurring from last_fired_at/created_at,
                # one-shot from created_at (see upstream rationale).
                if task.recurring:
                    computed = jittered_next_cron_run_ms(
                        task.cron, task.last_fired_at or task.created_at, task.id, cfg
                    )
                else:
                    computed = one_shot_jittered_next_cron_run_ms(task.cron, task.created_at, task.id, cfg)
                next_fire = math.inf if computed is None else computed
                self._next_fire_at[task.id] = next_fire

            if now < next_fire:
                return

            self._on_fire(task)

            aged = is_recurring_task_aged(task, now, cfg.recurring_max_age_ms)
            if task.recurring and not aged:
                # Reschedule from now (not from `next`) to avoid rapid catch-up.
                new_next = jittered_next_cron_run_ms(task.cron, now, task.id, cfg)
                self._next_fire_at[task.id] = math.inf if new_next is None else new_next
                task.last_fired_at = now
                if not is_session:
                    fired_durable_recurring.append(task.id)
            else:
                # One-shot (or aged-out recurring): delete after firing.
                self._store.remove([task.id])
                self._next_fire_at.pop(task.id, None)

        # Durable tasks only when we own the lock (single-writer guarantee).
        if self._is_owner:
            for task in self._durable:
                process(task, False)
            if fired_durable_recurring:
                self._persist_fired(fired_durable_recurring, now)

        # Session-only tasks are process-private — no lock needed, read fresh.
        for task in self._store.session_tasks():
            process(task, True)

        if not seen:
            self._next_fire_at.clear()
            return
        for task_id in list(self._next_fire_at.keys()):
            if task_id not in seen:
                self._next_fire_at.pop(task_id, None)

    def _persist_fired(self, ids: List[str], now: int) -> None:
        """Stamp ``last_fired_at`` on durable recurring tasks and write back (batched)."""
        id_set = set(ids)
        tasks = self._store.load()
        changed = False
        for task in tasks:
            if task.id in id_set:
                task.last_fired_at = now
                changed = True
        if changed:
            self._store.save(tasks)
            # Our own write bumps mtime; re-anchor so the next tick doesn't reload
            # (next_fire_at already holds the rescheduled time — idempotent).
            self._last_mtime = self._store.mtime()
            self._durable = tasks

    # ------------------------------------------------------------------
    # Missed compensation (startup)
    # ------------------------------------------------------------------
    def _run_missed(self) -> None:
        """Surface one-shot durable tasks whose window passed while we were down."""
        if not self._is_owner:
            return
        now = int(self._clock())
        missed: List[CronTask] = []
        for task in self._durable:
            if task.recurring or task.id in self._missed_asked:
                continue
            if self._task_filter is not None and not self._task_filter(task):
                continue
            nxt = _next_cron_run_ms(task.cron, task.created_at)
            if nxt is not None and nxt < now:
                missed.append(task)

        if not missed:
            return
        for task in missed:
            self._missed_asked.add(task.id)
            # Block _check from re-firing while removal lands.
            self._next_fire_at[task.id] = math.inf
        if self._on_missed is not None:
            self._on_missed(missed)
        else:
            for task in missed:
                self._on_fire(task)
        self._store.remove([task.id for task in missed])
        self._reload_durable(force=True)


__all__ = ["CronScheduler", "is_recurring_task_aged"]
