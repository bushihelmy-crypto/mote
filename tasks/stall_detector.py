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
from typing import TYPE_CHECKING, Optional

from metagpt.common.schema import BackgroundTaskNotification, BgStatus, CauseBy, MessagePriority
from metagpt.common.const.tasks import (
    STALL_CHECK_INTERVAL,
    STALL_THRESHOLD,
    STALL_TAIL_BYTES,
)

if TYPE_CHECKING:
    from metagpt.common.schema import MessageQueue
    from metagpt.tasks.pool import BackgroundTaskPool
    from metagpt.tasks.disk_output import TaskOutputStore

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

_TERMINAL_STATUSES = frozenset(
    {BgStatus.SUCCESS, BgStatus.FAILED, BgStatus.CANCELLED, BgStatus.TIMEOUT}
)


def _matches_interactive_prompt(text: str) -> bool:
    """Check whether *text* matches any known interactive prompt pattern."""
    return bool(_PROMPT_RE.search(text))


# ------------------------------------------------------------------
# StallDetector
# ------------------------------------------------------------------


class StallDetector:
    """Watch background tasks for stalling and push warnings.

    Usage::

        detector = StallDetector(pool, store, msg_buffer)
        detector.start_watching(task_id)
        # ... later ...
        detector.stop_watching(task_id)
        # or on shutdown:
        detector.stop_all()
    """

    def __init__(
        self,
        pool: "BackgroundTaskPool",
        store: "TaskOutputStore",
        msg_buffer: "MessageQueue",
        stall_check_interval: float = STALL_CHECK_INTERVAL,
        stall_threshold: float = STALL_THRESHOLD,
        stall_tail_bytes: int = STALL_TAIL_BYTES,
    ) -> None:
        self._pool = pool
        self._store = store
        self._msg_buffer = msg_buffer
        self._stall_check_interval = stall_check_interval
        self._stall_threshold = stall_threshold
        self._stall_tail_bytes = stall_tail_bytes
        self._watchers: dict[str, asyncio.Task] = {}

    def start_watching(self, task_id: str) -> None:
        """Begin monitoring *task_id* for stalling."""
        if task_id in self._watchers:
            return
        task = asyncio.create_task(self._monitor(task_id))
        self._watchers[task_id] = task

    def stop_watching(self, task_id: str) -> None:
        """Cancel the monitoring coroutine for *task_id*."""
        watcher = self._watchers.pop(task_id, None)
        if watcher is not None:
            watcher.cancel()

    def stop_all(self) -> None:
        """Cancel all active monitoring coroutines."""
        for watcher in self._watchers.values():
            watcher.cancel()
        self._watchers.clear()

    async def _monitor(self, task_id: str) -> None:
        """Periodically check output growth for *task_id*."""
        last_size: int = 0
        last_growth_time: float = asyncio.get_event_loop().time()
        stall_notified: bool = False

        try:
            while True:
                await asyncio.sleep(self._stall_check_interval)

                # Check if task still exists and is active
                meta = self._pool.get_task_info(task_id)
                if meta is None or meta.status in _TERMINAL_STATUSES:
                    break

                # Check output size
                try:
                    current_size = self._store.get_size(task_id)
                except KeyError:
                    continue  # store doesn't know about this task yet

                now = asyncio.get_event_loop().time()

                if current_size > last_size:
                    last_size = current_size
                    last_growth_time = now
                    stall_notified = False  # reset on new growth
                    continue

                # No growth — check if stall threshold exceeded
                stall_duration = now - last_growth_time
                if stall_duration >= self._stall_threshold and not stall_notified:
                    # Read tail and check for interactive prompt
                    try:
                        tail_bytes = await self._store.get_tail(
                            task_id, self._stall_tail_bytes
                        )
                        tail_text = tail_bytes.decode("utf-8", errors="replace")
                    except KeyError:
                        continue

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
                        self._msg_buffer.push(
                            notification, priority=MessagePriority.NEXT
                        )
                        stall_notified = True
        except asyncio.CancelledError:
            pass
        finally:
            self._watchers.pop(task_id, None)
