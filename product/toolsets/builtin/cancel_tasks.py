"""Cancel one current Agent-owned process-local background task.

Cancels the entire DAG. All in-flight nodes receive asyncio cancellation.
"""

from __future__ import annotations

from mote.contracts.async_work.command import LocalCancelDisposition
from mote.contracts.task.models import TaskId
from mote.contracts.tool.errors import ToolError
from mote.orchestration.background_tasks.status import BackgroundTaskStatus
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import GetBgPool

_MSG_UNKNOWN_TASK = "Unknown task_id: {task_id}"
_MSG_CANCEL_DONE = "Task {task_id} is already {status}, cannot cancel."
_MSG_CANCEL_SUCCESS = "Task {task_id} ({command_name}) cancelled.\n" "Reason: {reason}"


class CancelTasks(BaseTool):
    name = "CancelTasks"
    requires = ("get_bg_pool",)

    # Injected from Role by bind(): Role.get_bg_pool.
    get_bg_pool: GetBgPool

    async def call(
        self,
        *,
        task_id: str,
        reason: str | None = None,
    ) -> str:
        """Cancel a running process-local background task.

        The active attempt is cancelled and reaches a typed terminal settlement.

        Args:
            task_id: The task ID to cancel (e.g. "bg_3").
            reason: Optional reason for cancellation (shown in notifications).
        """
        pool = self.get_bg_pool()
        canonical_task_id = TaskId(task_id)
        meta = pool.get_task_info(canonical_task_id)
        if meta is None:
            raise ToolError(_MSG_UNKNOWN_TASK.format(task_id=task_id))

        if meta.status not in (
            BackgroundTaskStatus.PENDING,
            BackgroundTaskStatus.RUNNING,
        ):
            return _MSG_CANCEL_DONE.format(task_id=task_id, status=meta.status)

        receipt = pool.cancel_current(canonical_task_id, reason or "user requested")
        if receipt.disposition is not LocalCancelDisposition.CANCEL_REQUESTED:
            return _MSG_CANCEL_DONE.format(task_id=task_id, status="finished")

        # Cancelling is a consume: the model acted on the task, so retire its
        # re-projected result/marker and let the meta be reaped.
        pool.mark_retrieved(canonical_task_id)

        cancel_reason = reason or "user requested"

        return _MSG_CANCEL_SUCCESS.format(
            task_id=task_id,
            command_name=meta.command_name,
            reason=cancel_reason,
        )
