#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CronService — persistent time triggers dispatched through ``TriggerSink``.

It also fronts task management (create/list/delete) with validation: the cron
string must parse, must have a next fire within 366 days, and the total task
count is capped (mirroring the upstream 50-job limit).
"""

from __future__ import annotations

from typing import Callable, List, Optional

from mote.contracts.clock import UNIX_UTC_CLOCK
from mote.contracts.ports.clock import ClockSource
from mote.orchestration.automation import TriggerDisposition, TriggerReceipt, TriggerSink
from mote.orchestration.automation.cron.expression import _next_cron_run_ms, parse_cron_expression
from mote.orchestration.automation.cron.lock import SchedulerLock
from mote.orchestration.automation.cron.scheduler import CronScheduler
from mote.orchestration.automation.cron.store import CronOccurrence, CronTaskStore
from mote.orchestration.automation.cron.task import CronJitterConfig, CronTask, CronTriggerIntent
from mote.runtime.telemetry.logging import log_class

#: Upper bound on concurrent scheduled tasks (upstream MAX_JOBS).
MAX_CRON_TASKS = 50
#: A task whose next fire is beyond this horizon is rejected (cron walk bound).
_MAX_HORIZON_MS = 366 * 24 * 60 * 60 * 1000


def _now_ms(clock_source: ClockSource) -> int:
    instant = clock_source.now()
    instant.require_clock(UNIX_UTC_CLOCK)
    return instant.epoch_nanoseconds // 1_000_000


def validate_new_task(
    cron: str,
    *,
    now_ms: int,
    timezone_name: str = "UTC",
) -> None:
    """Raise ``ValueError`` if a new task with ``cron`` cannot be admitted.

    The control-free admission gate shared by :meth:`CronService.create_task`
    (in-process) and the ``mote cron add`` CLI (no live control): the cron string
    must parse, must have a next fire within the 366-day horizon, and the total
    task count must be under :data:`MAX_CRON_TASKS`. Keeping it a free function
    means the CLI reuses the exact same rules without constructing an
    ``AgentControl``.
    """
    if parse_cron_expression(cron) is None:
        raise ValueError(f"invalid cron expression: {cron!r}")
    now = now_ms
    nxt = _next_cron_run_ms(cron, now, timezone_name=timezone_name)
    if nxt is None or nxt - now > _MAX_HORIZON_MS:
        raise ValueError(f"cron {cron!r} has no fire time within the next 366 days")


class CronTaskCommands:
    """Canonical transactional task-management surface for every Product entrypoint."""

    def __init__(
        self,
        store: CronTaskStore,
        *,
        default_timezone_name: str,
        clock_source: ClockSource,
    ) -> None:
        self._store = store
        self._default_timezone_name = default_timezone_name
        self._clock_source = clock_source

    def create(
        self,
        cron: str,
        prompt: str,
        target_session_id: str | None,
        *,
        recurring: bool = False,
        durable: bool = True,
        agent_id: str | None = None,
        timezone_name: str | None = None,
    ) -> CronTask:
        selected_timezone = timezone_name or self._default_timezone_name
        now = _now_ms(self._clock_source)
        validate_new_task(cron, now_ms=now, timezone_name=selected_timezone)
        return self._store.add(
            CronTask.new(
                cron,
                prompt,
                now,
                recurring=recurring,
                durable=durable,
                agent_id=agent_id,
                target_session_id=target_session_id,
                timezone_name=selected_timezone,
            ),
            capacity_limit=MAX_CRON_TASKS,
        )

    def list(self, *, agent_id: str | None = None) -> List[CronTask]:
        tasks = self._store.list()
        return tasks if agent_id is None else [task for task in tasks if task.agent_id == agent_id]

    def remove(self, ids: List[str]) -> int:
        return self._store.remove(ids)


@log_class(level="DEBUG")
class CronService:
    """Own the store/lock/scheduler trio and emit generic automation triggers."""

    def __init__(
        self,
        trigger_sink: TriggerSink,
        *,
        session_id: Optional[str] = None,
        base_dir: Optional[str] = None,
        default_timezone_name: str,
        jitter_config: Optional[Callable[[], CronJitterConfig]] = None,
        clock_source: ClockSource,
    ):
        self._trigger_sink = trigger_sink
        self._session_id = session_id or "cron"
        self._default_timezone_name = default_timezone_name
        self._store = CronTaskStore(base_dir=base_dir)
        self._commands = CronTaskCommands(
            self._store,
            default_timezone_name=default_timezone_name,
            clock_source=clock_source,
        )
        lock = SchedulerLock(
            self._session_id,
            base_dir=str(self._store.path.parent),
            clock_source=clock_source,
        )
        self._scheduler = CronScheduler(
            self._store,
            on_fire=self._on_fire,
            lock=lock,
            jitter_config=jitter_config,
            clock_source=clock_source,
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
        timezone_name: str | None = None,
    ) -> CronTask:
        """Validate and register a scheduled task.

        Raises ``ValueError`` on an invalid cron string, a next fire beyond the
        366-day horizon, or when the task cap is reached.
        """
        return self._commands.create(
            cron,
            prompt,
            target_session_id,
            recurring=recurring,
            durable=durable,
            agent_id=agent_id,
            timezone_name=timezone_name,
        )

    def list_tasks(self, *, agent_id: Optional[str] = None) -> List[CronTask]:
        """All tasks, optionally filtered to a single creating ``agent_id``."""
        return self._commands.list(agent_id=agent_id)

    def delete_tasks(self, ids: List[str]) -> int:
        """Remove tasks by id; returns the number removed."""
        return self._commands.remove(ids)

    # ------------------------------------------------------------------
    # Scheduler hooks
    # ------------------------------------------------------------------
    def _on_fire(
        self,
        task: CronTask,
        occurrence: CronOccurrence,
    ) -> TriggerReceipt:
        """Dispatch a due task without knowing what kind of target consumes it.

        A task with no explicit ``target_session_id`` (e.g. one created off-process
        via ``mote cron add``) fires into the session that owns this scheduler —
        whatever live session started the service — so CLI-authored tasks reach the
        running agent without the CLI needing to know a session id at write time.
        """
        target = task.target_session_id or self._session_id
        if not target:
            return TriggerReceipt(
                TriggerDisposition.REJECTED,
                reason="cron trigger has no target",
            )
        trigger = CronTriggerIntent(
            schema_version=1,
            task_id=task.id,
            task_revision=occurrence.task_revision,
            target=target,
            content=task.prompt,
            scheduled_at_ms=occurrence.scheduled_at_ms,
            fired_at_ms=occurrence.observed_at_ms,
            attempt=occurrence.attempt,
        ).to_automation_trigger()
        if trigger.trigger_id != occurrence.occurrence_id:
            raise RuntimeError("Cron trigger identity diverged from durable occurrence")
        return self._trigger_sink.dispatch(trigger)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._scheduler.start()

    async def stop(self) -> None:
        await self._scheduler.stop()


__all__ = ["CronService", "CronTaskCommands", "MAX_CRON_TASKS", "validate_new_task"]
