"""Stall detector for background tasks.

Monitors running background tasks for signs of stalling (no output growth)
and interactive prompts that require user intervention.  Aligned with
Claude Code's stall watchdog (``LocalShellTask.tsx``).

This module is standalone — it does **not** modify ``BackgroundTaskPool``,
``RoleZero``, or the ``_observe()`` loop.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from mote.common.const.tasks import STALL_CHECK_INTERVAL, STALL_TAIL_BYTES, STALL_THRESHOLD
from mote.common.scheduling import PeriodicLoop
from mote.common.schema import CauseBy
from mote.executor.tasks.types import BackgroundTaskNotification, BgStatus

if TYPE_CHECKING:
    from mote.executor.tasks.disk_output import TaskOutputStore
    from mote.executor.tasks.pool import BackgroundTaskPool

# Interactive prompt patterns (aligned with Claude Code)
_PROMPT_PATTERNS = [
    r"\(y/n\)",
    r"\[y/n\]",
    r"\(yes/no\)",
    r"Press\s+(any key|Enter)",
    r"Continue\?",
    r"Overwrite\?",
    r"[Pp]assword\s*:",
]

_PROMPT_RE = re.compile("|".join(_PROMPT_PATTERNS))

_TERMINAL_STATUSES = frozenset({BgStatus.SUCCESS, BgStatus.FAILED, BgStatus.CANCELLED, BgStatus.TIMEOUT})


def _matches_interactive_prompt(text: str) -> bool:
    """Check whether *text* matches any known interactive prompt pattern."""
    return bool(_PROMPT_RE.search(text))


# ------------------------------------------------------------------
# StallDetector
# ------------------------------------------------------------------


class StallDetector:
    """Watch background tasks for stalling and push warnings.

    Usage::

        detector = StallDetector(pool, store)
        detector.start_watching(task_id)
        # ... later ...
        detector.stop_watching(task_id)
        # or on shutdown:
        detector.stop_all()

    Warnings are delivered through ``pool.deliver`` — the single push+wake
    choke point — so a stall detected *between* turns also wakes a parked
    scheduler driver, not just a mid-turn waiter.
    """

    def __init__(
        self,
        pool: "BackgroundTaskPool",
        store: "TaskOutputStore",
        stall_check_interval: float = STALL_CHECK_INTERVAL,
        stall_threshold: float = STALL_THRESHOLD,
        stall_tail_bytes: int = STALL_TAIL_BYTES,
    ) -> None:
        self._pool = pool
        self._store = store
        self._stall_check_interval = stall_check_interval
        self._stall_threshold = stall_threshold
        self._stall_tail_bytes = stall_tail_bytes
        self._watchers: dict[str, PeriodicLoop] = {}

    def start_watching(self, task_id: str) -> None:
        """Begin monitoring *task_id* for stalling."""
        existing = self._watchers.get(task_id)
        if existing is not None and existing.is_running():
            return
        loop = PeriodicLoop(
            self._stall_check_interval,
            self._make_tick(task_id),
            name=f"stall-detector:{task_id}",
            sleep_first=True,  # sleep before the first check (match upstream cadence)
        )
        self._watchers[task_id] = loop
        loop.start()

    def stop_watching(self, task_id: str) -> None:
        """Cancel the monitoring loop for *task_id*."""
        watcher = self._watchers.pop(task_id, None)
        if watcher is not None:
            watcher.cancel()

    def stop_all(self) -> None:
        """Cancel all active monitoring loops."""
        for watcher in self._watchers.values():
            watcher.cancel()
        self._watchers.clear()

    def _make_tick(self, task_id: str):
        """Build the per-task tick closure carrying its own growth-tracking state."""
        state = {"last_size": 0, "last_growth": None, "notified": False}

        async def _tick():
            now = asyncio.get_event_loop().time()
            if state["last_growth"] is None:
                state["last_growth"] = now

            # Stop (and self-remove) once the task reaches a terminal status.
            meta = self._pool.get_task_info(task_id)
            if meta is None or meta.status in _TERMINAL_STATUSES:
                self._watchers.pop(task_id, None)
                return False

            try:
                current_size = self._store.get_size(task_id)
            except KeyError:
                return  # store doesn't know about this task yet

            if current_size > state["last_size"]:
                state["last_size"] = current_size
                state["last_growth"] = now
                state["notified"] = False  # reset on new growth
                return

            # No growth — check if the stall threshold is exceeded.
            stall_duration = now - state["last_growth"]
            if stall_duration < self._stall_threshold or state["notified"]:
                return

            try:
                tail_bytes = await self._store.get_tail(task_id, self._stall_tail_bytes)
                tail_text = tail_bytes.decode("utf-8", errors="replace")
            except KeyError:
                return

            if _matches_interactive_prompt(tail_text):
                command_name = meta.command_name if meta else task_id
                notification = BackgroundTaskNotification(
                    content=(
                        f"<task-notification>\n"
                        f"<task-id>{task_id}</task-id>\n"
                        f"<status>stall_warning</status>\n"
                        f"<summary>{command_name} appears stalled — "
                        f"possible interactive prompt detected</summary>\n"
                        f"</task-notification>"
                    ),
                    cause_by=CauseBy.RUN_COMMAND,
                    task_id=task_id,
                    command_name=command_name,
                    status="stall_warning",
                    result=tail_text[-200:],
                )
                self._pool.deliver(notification)
                state["notified"] = True

        return _tick
