"""cancel_tasks — Cancel running background pipeline tasks.

Cancels the entire DAG. All in-flight nodes receive asyncio cancellation.
Already-completed node results are preserved in the state snapshot, so the
task can be resumed later via resume_tasks.
"""
from __future__ import annotations

from typing import Optional

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.executor.tasks.types import BgStatus

_MSG_UNKNOWN_TASK = "Unknown task_id: {task_id}"
_MSG_CANCEL_DONE = "Task {task_id} is already {status}, cannot cancel."
_MSG_CANCEL_SUCCESS = (
    "Task {task_id} ({command_name}) cancelled.\n"
    "Reason: {reason}\n"
    "Use resume_tasks(task_id='{task_id}', from_node='...') to resume."
)


@register_tool
class CancelTasks(BaseTool):
    name = "CancelTasks"
    aliases = ["cancel_tasks"]
    description = (
        "Cancel a running background pipeline task. The entire DAG is cancelled; "
        "already-completed node results are preserved and the task can be resumed later."
    )
    requires = ("get_bg_pool",)

    async def call(
        self,
        *,
        task_id: str,
        reason: str | None = None,
    ) -> str:
        """Cancel a running background task.

        Args:
            task_id: The task ID to cancel (e.g. "bg_3").
            reason: Optional reason for cancellation (shown in notifications).
        """
        pool = self.get_bg_pool()
        meta = pool.get_task_info(task_id)
        if meta is None:
            raise ToolError(_MSG_UNKNOWN_TASK.format(task_id=task_id))

        # Can only cancel tasks that are still active
        if meta.status not in (BgStatus.PENDING, BgStatus.RUNNING, BgStatus.WAITING_FOR_ROUTE):
            return _MSG_CANCEL_DONE.format(task_id=task_id, status=meta.status)

        success = pool.cancel(task_id)
        if not success:
            return _MSG_CANCEL_DONE.format(task_id=task_id, status="finished")

        cancel_reason = reason or "user requested"

        return _MSG_CANCEL_SUCCESS.format(
            task_id=task_id,
            command_name=meta.command_name,
            reason=cancel_reason,
        )
