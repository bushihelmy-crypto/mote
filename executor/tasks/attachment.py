"""Generate task attachments by polling running/completed background tasks.

Aligned with Claude Code's ``framework.ts`` (``generateTaskAttachments`` /
``applyTaskOffsetsAndEvictions``).  This module is standalone — it does **not**
modify ``BackgroundTaskPool``, ``Role``, or the ``_observe()`` loop.

The caller invokes ``TaskAttachmentGenerator.generate()`` before each LLM
query and decides how to consume the returned ``GenerateResult``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as _escape_xml

from mote.common.const.tasks import DELTA_MAX_BYTES, DELTA_SUMMARY_MAX_CHARS
from mote.common.exception import ErrorReport, render_error_block
from mote.common.text import format_elapsed
from mote.executor.tasks.types import BgStatus

if TYPE_CHECKING:
    from mote.executor.tasks.disk_output import TaskOutputStore
    from mote.executor.tasks.pool import BackgroundTaskPool

_TERMINAL_STATUSES = frozenset({BgStatus.SUCCESS, BgStatus.FAILED, BgStatus.CANCELLED, BgStatus.TIMEOUT})


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------


@dataclass
class TaskAttachment:
    """Single attachment describing a background task's current state."""

    task_id: str
    status: str  # BgStatus value
    command_name: str
    description: str  # e.g. "generate videos is running (elapsed: 45.2s)"
    delta_summary: str | None  # incremental output summary, None = no new output
    # Structured failure record (ErrorReport.as_dict form) for a FAILED task,
    # carried from the pool's TaskMeta. None when the task did not fail or
    # carries no structured error.
    error: dict | None = None


@dataclass
class GenerateResult:
    """Return value of ``TaskAttachmentGenerator.generate()``."""

    attachments: list[TaskAttachment] = field(default_factory=list)
    evicted_task_ids: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# Generator
# ------------------------------------------------------------------


class TaskAttachmentGenerator:
    """Poll running tasks and generate delta attachments.

    Standalone — does not push to msg_buffer.  Caller decides consumption.
    """

    def __init__(
        self,
        pool: "BackgroundTaskPool",
        store: "TaskOutputStore | None" = None,
    ) -> None:
        self._pool = pool
        self._store = store
        self._offsets: dict[str, int] = {}
        self._notified: set[str] = set()

    def mark_notified(self, task_id: str) -> None:
        """Externally mark a task as having been consumed."""
        self._notified.add(task_id)

    async def generate(self) -> GenerateResult:
        """Scan all tracked tasks and build attachments.

        * Running tasks produce an attachment with optional delta_summary.
        * First-time terminal tasks produce a final attachment and are marked
          *notified*.
        * Previously-notified terminal tasks are evicted so the caller can
          clean up resources.
        """
        attachments: list[TaskAttachment] = []
        evicted_task_ids: list[str] = []

        for meta in self._pool.list_tasks():
            tid = meta.task_id
            is_terminal = meta.status in _TERMINAL_STATUSES

            # 1. Already-notified terminal → evict
            if tid in self._notified and is_terminal:
                evicted_task_ids.append(tid)
                self._offsets.pop(tid, None)
                self._notified.discard(tid)
                continue

            # 2. Pending (waiting for semaphore) → simple status attachment
            if meta.status == BgStatus.PENDING:
                elapsed = time.time() - meta.submit_time
                attachments.append(
                    TaskAttachment(
                        task_id=tid,
                        status=meta.status,
                        command_name=meta.command_name,
                        description=(f"{meta.command_name} is pending " f"(queued: {format_elapsed(elapsed)})"),
                        delta_summary=None,
                    )
                )
                continue

            # 3. Running → read incremental delta
            if meta.status == BgStatus.RUNNING:
                delta_summary = None
                if self._store is not None:
                    try:
                        delta_bytes, new_off = await self._store.get_delta(
                            tid, self._offsets.get(tid, 0), DELTA_MAX_BYTES
                        )
                        if delta_bytes:
                            self._offsets[tid] = new_off
                            delta_summary = delta_bytes.decode("utf-8", errors="replace")[:DELTA_SUMMARY_MAX_CHARS]
                    except KeyError:
                        pass  # store has no output for this task
                elapsed = time.time() - meta.start_time
                attachments.append(
                    TaskAttachment(
                        task_id=tid,
                        status=meta.status,
                        command_name=meta.command_name,
                        description=(f"{meta.command_name} is running " f"(elapsed: {format_elapsed(elapsed)})"),
                        delta_summary=delta_summary,
                    )
                )

            # 4. First-time terminal → final attachment + mark notified
            #    If _on_done() already pushed a BackgroundTaskNotification
            #    (meta.notified=True), skip generating a duplicate attachment
            #    and just mark internally so we evict next round.
            elif is_terminal and tid not in self._notified:
                if getattr(meta, "notified", False):
                    self._notified.add(tid)
                    continue
                delta_summary = None
                if self._store is not None:
                    try:
                        tail = await self._store.get_tail(tid, DELTA_MAX_BYTES)
                        if tail:
                            delta_summary = tail.decode("utf-8", errors="replace")[:DELTA_SUMMARY_MAX_CHARS]
                    except KeyError:
                        pass
                elapsed = (meta.end_time or time.time()) - meta.start_time
                status_str = meta.status.value if isinstance(meta.status, BgStatus) else str(meta.status)
                attachments.append(
                    TaskAttachment(
                        task_id=tid,
                        status=meta.status,
                        command_name=meta.command_name,
                        description=(f"{meta.command_name} {status_str} " f"(elapsed: {format_elapsed(elapsed)})"),
                        delta_summary=delta_summary,
                        error=meta.error,
                    )
                )
                self._notified.add(tid)

        return GenerateResult(
            attachments=attachments,
            evicted_task_ids=evicted_task_ids,
        )


# ------------------------------------------------------------------
# XML formatting
# ------------------------------------------------------------------


def format_attachment_xml(att: TaskAttachment) -> str:
    """Render a ``TaskAttachment`` as a ``<task-attachment>`` XML block."""
    status_str = att.status.value if isinstance(att.status, BgStatus) else str(att.status)
    lines = [
        "<task-attachment>",
        f"<task-id>{att.task_id}</task-id>",
        f"<command>{_escape_xml(att.command_name)}</command>",
        f"<status>{status_str}</status>",
        f"<description>{_escape_xml(att.description)}</description>",
    ]
    if att.delta_summary is not None:
        lines.append(f"<delta-summary>{_escape_xml(att.delta_summary)}</delta-summary>")
    if att.error is not None:
        # Reuse the single shared error renderer so a failed task's attachment
        # carries the same <error> block (code/recovery/detail) as its
        # notification.

        lines.append(render_error_block(ErrorReport.from_dict(att.error)))
    lines.append("</task-attachment>")
    return "\n".join(lines)
