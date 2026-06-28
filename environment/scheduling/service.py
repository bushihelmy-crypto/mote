#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CronService — glue between the pure :class:`CronScheduler` and :class:`AgentControl`.

The scheduler is intentionally control-plane-agnostic; this service owns the
wiring. When a task fires, the service injects its prompt into the target agent
as a fresh turn — exactly as if the user had sent a message at that moment::

    on_fire(task) -> control.send_input(task.target_session_id, UserMessage(prompt),
                                        mode=DeliveryMode.TRIGGER_TURN)

It also fronts task management (create/list/delete) with validation: the cron
string must parse, must have a next fire within 366 days, and the total task
count is capped (mirroring the upstream 50-job limit).
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

from metagpt.common.logs import log_class, logger
from metagpt.common.schema import UserMessage
from metagpt.environment.control import AgentControl
from metagpt.environment.mailbox import DeliveryMode
from metagpt.environment.scheduling.cron import _next_cron_run_ms, parse_cron_expression
from metagpt.environment.scheduling.lock import SchedulerLock
from metagpt.environment.scheduling.scheduler import CronScheduler
from metagpt.environment.scheduling.store import CronTaskStore
from metagpt.environment.scheduling.task import CronJitterConfig, CronTask

#: Upper bound on concurrent scheduled tasks (upstream MAX_JOBS).
MAX_CRON_TASKS = 50
#: A task whose next fire is beyond this horizon is rejected (cron walk bound).
_MAX_HORIZON_MS = 366 * 24 * 60 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


@log_class(level="DEBUG")
class CronService:
    """Owns the store/lock/scheduler trio and routes fires to ``AgentControl``."""

    def __init__(
        self,
        control: AgentControl,
        *,
        session_id: Optional[str] = None,
        base_dir: Optional[str] = None,
        jitter_config: Optional[Callable[[], CronJitterConfig]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self._control = control
        self._session_id = session_id or getattr(control, "session_id", None) or "cron"
        self._store = CronTaskStore(base_dir=base_dir)
        lock = SchedulerLock(self._session_id, base_dir=str(self._store.path.parent))
        self._scheduler = CronScheduler(
            self._store,
            on_fire=self._on_fire,
            is_idle=self._is_idle,
            lock=lock,
            jitter_config=jitter_config,
            clock=clock,
        )

    @property
    def store(self) -> CronTaskStore:
        return self._store

    @property
    def scheduler(self) -> CronScheduler:
        return self._scheduler

    # ------------------------------------------------------------------
    # Task management
    # ------------------------------------------------------------------
    def create_task(
        self,
        cron: str,
        prompt: str,
        target_session_id: str,
        *,
        recurring: bool = False,
        durable: bool = True,
        agent_id: Optional[str] = None,
    ) -> CronTask:
        """Validate and register a scheduled task.

        Raises ``ValueError`` on an invalid cron string, a next fire beyond the
        366-day horizon, or when the task cap is reached.
        """
        if parse_cron_expression(cron) is None:
            raise ValueError(f"invalid cron expression: {cron!r}")
        now = _now_ms()
        nxt = _next_cron_run_ms(cron, now)
        if nxt is None or nxt - now > _MAX_HORIZON_MS:
            raise ValueError(f"cron {cron!r} has no fire time within the next 366 days")
        if len(self._store.list()) >= MAX_CRON_TASKS:
            raise ValueError(f"scheduled task limit reached ({MAX_CRON_TASKS})")

        task = CronTask.new(
            cron,
            prompt,
            now,
            recurring=recurring,
            durable=durable,
            agent_id=agent_id,
            target_session_id=target_session_id,
        )
        self._store.add(task)
        return task

    def list_tasks(self, *, agent_id: Optional[str] = None) -> List[CronTask]:
        """All tasks, optionally filtered to a single creating ``agent_id``."""
        tasks = self._store.list()
        if agent_id is not None:
            tasks = [t for t in tasks if t.agent_id == agent_id]
        return tasks

    def delete_tasks(self, ids: List[str]) -> int:
        """Remove tasks by id; returns the number removed."""
        return self._store.remove(ids)

    # ------------------------------------------------------------------
    # Scheduler hooks
    # ------------------------------------------------------------------
    def _on_fire(self, task: CronTask) -> None:
        """Inject the task's prompt into its target agent as a new turn."""
        target = task.target_session_id
        if not target:
            return
        message = UserMessage(content=task.prompt)
        try:
            self._control.send_input(target, message, mode=DeliveryMode.TRIGGER_TURN)
        except Exception as exc:  # noqa: BLE001 — target may be gone; best-effort, never raise
            logger.debug(f"CronService: fire of task into {target} dropped: {exc}")

    def _is_idle(self) -> bool:
        """True when no targeted runtime is mid-turn (safe to deliver)."""
        runtimes = self._control.runtimes()
        for runtime in runtimes.values():
            if getattr(runtime, "active_turn", False):
                return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._scheduler.start()

    async def stop(self) -> None:
        await self._scheduler.stop()


__all__ = ["CronService", "MAX_CRON_TASKS"]
