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

import math
from typing import Callable, Dict, List, Optional

from mote.contracts.clock import UNIX_UTC_CLOCK
from mote.contracts.ports.clock import ClockSource
from mote.orchestration.automation import TriggerDisposition, TriggerReceipt
from mote.orchestration.automation.cron.expression import (
    _next_cron_run_ms,
    jittered_next_cron_run_ms,
    one_shot_jittered_next_cron_run_ms,
)
from mote.orchestration.automation.cron.lock import SchedulerFence, SchedulerFenceLost, SchedulerLock
from mote.orchestration.automation.cron.store import CronOccurrence, CronOccurrenceState, CronTaskStore
from mote.orchestration.automation.cron.task import DEFAULT_CRON_JITTER_CONFIG, CronJitterConfig, CronTask
from mote.runtime.control.scheduling import PeriodicLoop
from mote.runtime.telemetry.logging import log_class


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
        on_fire: Callable[[CronTask, CronOccurrence], TriggerReceipt],
        *,
        is_idle: Optional[Callable[[], bool]] = None,
        on_missed: Optional[Callable[[List[CronTask]], None]] = None,
        is_killed: Optional[Callable[[], bool]] = None,
        task_filter: Optional[Callable[[CronTask], bool]] = None,
        lock: Optional[SchedulerLock] = None,
        jitter_config: Optional[Callable[[], CronJitterConfig]] = None,
        clock_source: ClockSource,
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
        self._clock_source = clock_source

        # Per-task next-fire times (epoch ms; ``math.inf`` == "never").
        self._next_fire_at: Dict[str, float] = {}
        # Ids already surfaced as missed — avoids re-asking before removal lands.
        self._missed_asked: set[str] = set()
        # Cached durable tasks + the mtime they were loaded at (hot-reload anchor).
        self._durable: List[CronTask] = []
        self._last_mtime: Optional[float] = None
        self._is_owner = False
        self._fence: SchedulerFence | None = None
        self._runner = PeriodicLoop(
            check_interval,
            self._check,
            name="cron-scheduler",
        )

    def _now_ms(self) -> int:
        instant = self._clock_source.now()
        instant.require_clock(UNIX_UTC_CLOCK)
        return instant.epoch_nanoseconds // 1_000_000

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Acquire the lock (if any), run missed compensation, and start the loop."""
        self._fence = self._lock.acquire() if self._lock is not None else None
        self._is_owner = self._fence is not None
        self._reload_durable(force=True)
        self._run_missed()
        self._runner.start()

    async def stop(self) -> None:
        """Cancel the loop and release the lock."""
        await self._runner.stop()
        if self._lock is not None and self._is_owner:
            self._lock.release()
        self._is_owner = False
        self._fence = None

    def next_fire_time(self) -> Optional[int]:
        """Soonest scheduled fire (epoch ms) across loaded tasks, or ``None``."""
        soonest = math.inf
        for value in self._next_fire_at.values():
            if value < soonest:
                soonest = value
        return None if soonest == math.inf else int(soonest)

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

        if self._lock is not None and self._is_owner:
            try:
                self._lock.refresh()
            except SchedulerFenceLost:
                self._is_owner = False
                self._fence = None

        now = self._now_ms()
        cfg = self._get_cfg()
        self._reload_durable()

        seen: set[str] = set()

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
                        task.cron,
                        task.last_fired_at or task.created_at,
                        task.id,
                        cfg,
                        timezone_name=task.timezone_name,
                        dst_policy=task.dst_policy,
                    )
                else:
                    computed = one_shot_jittered_next_cron_run_ms(
                        task.cron,
                        task.created_at,
                        task.id,
                        cfg,
                        timezone_name=task.timezone_name,
                        dst_policy=task.dst_policy,
                    )
                next_fire = math.inf if computed is None else computed
                self._next_fire_at[task.id] = next_fire

            if now < next_fire:
                return

            aged = is_recurring_task_aged(task, now, cfg.recurring_max_age_ms)
            delete_on_accept = not task.recurring or aged
            if is_session:
                accepted = self._dispatch_session(
                    task,
                    scheduled_at_ms=int(next_fire),
                    observed_at_ms=now,
                )
                if not accepted:
                    return
                if delete_on_accept:
                    self._store.remove_session_task(str(task.id))
                    self._next_fire_at.pop(task.id, None)
                else:
                    task = self._store.settle_session_task(
                        task.id,
                        expected_revision=task.revision,
                        settled_at_ms=now,
                    )
                    new_next = jittered_next_cron_run_ms(
                        task.cron,
                        now,
                        task.id,
                        cfg,
                        timezone_name=task.timezone_name,
                        dst_policy=task.dst_policy,
                    )
                    self._next_fire_at[task.id] = math.inf if new_next is None else new_next
            else:
                state = self._dispatch_durable(
                    task,
                    scheduled_at_ms=int(next_fire),
                    observed_at_ms=now,
                    delete_on_accept=delete_on_accept,
                )
                if state is CronOccurrenceState.ACCEPTED:
                    if delete_on_accept:
                        self._next_fire_at.pop(task.id, None)
                    else:
                        new_next = jittered_next_cron_run_ms(
                            task.cron,
                            now,
                            task.id,
                            cfg,
                            timezone_name=task.timezone_name,
                            dst_policy=task.dst_policy,
                        )
                        self._next_fire_at[task.id] = math.inf if new_next is None else new_next
                    self._last_mtime = self._store.mtime()
                    self._durable = self._store.load()
                elif state is CronOccurrenceState.DEFERRED:
                    snapshot = self._store.load_snapshot()
                    occurrence = next(
                        item
                        for item in snapshot.occurrences
                        if item.task_id == str(task.id) and item.task_revision == task.revision
                    )
                    self._next_fire_at[task.id] = occurrence.next_attempt_at_ms or math.inf
                else:
                    self._next_fire_at[task.id] = math.inf

        # Durable tasks only when we own the lock (single-writer guarantee).
        if self._is_owner:
            for task in self._durable:
                process(task, False)

        # Session-only tasks are process-private — no lock needed, read fresh.
        for task in self._store.session_tasks():
            process(task, True)

        if not seen:
            self._next_fire_at.clear()
            return
        for task_id in list(self._next_fire_at.keys()):
            if task_id not in seen:
                self._next_fire_at.pop(task_id, None)

    def _dispatch_durable(
        self,
        task: CronTask,
        *,
        scheduled_at_ms: int,
        observed_at_ms: int,
        delete_on_accept: bool,
    ) -> CronOccurrenceState:
        fence = self._fence
        if fence is None:
            raise SchedulerFenceLost("durable cron dispatch requires scheduler ownership")
        snapshot = self._store.load_snapshot()
        existing = next(
            (
                item
                for item in snapshot.occurrences
                if item.task_id == str(task.id)
                and item.task_revision == task.revision
                and item.state is not CronOccurrenceState.ACCEPTED
            ),
            None,
        )
        if existing is not None:
            scheduled_at_ms = existing.scheduled_at_ms
        occurrence = self._store.claim_occurrence(
            fence=fence,
            task_id=str(task.id),
            expected_task_revision=task.revision,
            scheduled_at_ms=scheduled_at_ms,
            observed_at_ms=observed_at_ms,
            delete_on_accept=delete_on_accept,
        )
        dispatch = self._store.begin_dispatch(
            occurrence.occurrence_id,
            fence=fence,
            now_ms=observed_at_ms,
        )
        if dispatch.state is not CronOccurrenceState.DISPATCHING:
            return dispatch.state
        try:
            receipt = self._on_fire(task, dispatch)
        except Exception as error:  # noqa: BLE001 - unknown external outcome is durable
            settled = self._store.mark_in_doubt(
                dispatch.occurrence_id,
                fence=fence,
                expected_attempt=dispatch.attempt,
                reason=f"{type(error).__name__}: {error}",
            )
            return settled.state
        settled = self._store.settle_receipt(
            dispatch.occurrence_id,
            fence=fence,
            expected_attempt=dispatch.attempt,
            receipt=receipt,
            settled_at_ms=observed_at_ms,
        )
        return settled.state

    def _dispatch_session(
        self,
        task: CronTask,
        *,
        scheduled_at_ms: int,
        observed_at_ms: int,
    ) -> bool:
        occurrence = CronOccurrence(
            occurrence_id=f"cron:{task.id}:{task.revision}:{scheduled_at_ms}",
            task_id=str(task.id),
            task_revision=task.revision,
            scheduled_at_ms=scheduled_at_ms,
            observed_at_ms=observed_at_ms,
            state=CronOccurrenceState.DISPATCHING,
            attempt=1,
            receipt_id=None,
            reason=None,
            next_attempt_at_ms=None,
            delete_on_accept=False,
        )
        try:
            receipt = self._on_fire(task, occurrence)
        except Exception:  # process-local unknown outcome: retain and stop automatic retry
            self._next_fire_at[task.id] = math.inf
            return False
        if receipt.disposition is TriggerDisposition.ACCEPTED and receipt.receipt_id:
            return True
        if receipt.disposition is TriggerDisposition.DEFERRED:
            self._next_fire_at[task.id] = observed_at_ms + 1_000
        else:
            self._next_fire_at[task.id] = math.inf
        return False

    # ------------------------------------------------------------------
    # Missed compensation (startup)
    # ------------------------------------------------------------------
    def _run_missed(self) -> None:
        """Surface one-shot durable tasks whose window passed while we were down."""
        if not self._is_owner:
            return
        now = self._now_ms()
        missed: List[CronTask] = []
        for task in self._durable:
            if task.recurring or task.id in self._missed_asked:
                continue
            if self._task_filter is not None and not self._task_filter(task):
                continue
            nxt = _next_cron_run_ms(
                task.cron,
                task.created_at,
                timezone_name=task.timezone_name,
                dst_policy=task.dst_policy,
            )
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
            return
        else:
            for task in missed:
                nxt = _next_cron_run_ms(
                    task.cron,
                    task.created_at,
                    timezone_name=task.timezone_name,
                    dst_policy=task.dst_policy,
                )
                if nxt is not None:
                    self._dispatch_durable(
                        task,
                        scheduled_at_ms=nxt,
                        observed_at_ms=now,
                        delete_on_accept=True,
                    )
        self._reload_durable(force=True)


__all__ = ["CronScheduler", "is_recurring_task_aged"]
