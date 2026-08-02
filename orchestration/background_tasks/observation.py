"""Total presentation projections and owner-bound local async-work adapter."""

from __future__ import annotations

from mote.contracts.async_work.command import CancelLocalBackgroundTask, LocalCancelDisposition, LocalCancelReceipt
from mote.contracts.async_work.identity import LocalBackgroundTaskReference
from mote.contracts.async_work.observation import (
    AsyncWorkAction,
    AsyncWorkPresentationPhase,
    LocalBackgroundObservationDetail,
    LocalBackgroundTaskObservation,
)
from mote.contracts.ports.async_work.observation import AsyncWorkQueryDisposition, AsyncWorkQueryResult
from mote.contracts.task.models import (
    CommandName,
    CompletedInlineTaskResultPointer,
    FailedTaskResultPointer,
    InlineTaskOutput,
    TaskFailure,
)
from mote.orchestration.background_tasks.pool import BackgroundTaskPool
from mote.orchestration.background_tasks.status import TERMINAL_STATUSES, BackgroundTaskStatus


def project_background_task_phase(
    status: BackgroundTaskStatus,
) -> AsyncWorkPresentationPhase:
    return {
        BackgroundTaskStatus.PENDING: AsyncWorkPresentationPhase.QUEUED,
        BackgroundTaskStatus.RUNNING: AsyncWorkPresentationPhase.RUNNING,
        BackgroundTaskStatus.SUCCESS: AsyncWorkPresentationPhase.SUCCEEDED,
        BackgroundTaskStatus.FAILED: AsyncWorkPresentationPhase.FAILED,
        BackgroundTaskStatus.CANCELLED: AsyncWorkPresentationPhase.CANCELLED,
        BackgroundTaskStatus.TIMEOUT: AsyncWorkPresentationPhase.TIMED_OUT,
        BackgroundTaskStatus.SKIPPED: AsyncWorkPresentationPhase.SUCCEEDED,
    }[status]


def _actions(status: BackgroundTaskStatus) -> tuple[AsyncWorkAction, ...]:
    if status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING}:
        return (AsyncWorkAction.CANCEL,)
    return (AsyncWorkAction.VIEW_RESULT,)


class AgentOwnedLocalAsyncWorkAdapter:
    """Queries one Pool only; it never owns or mirrors Pool state."""

    def __init__(self, pool: BackgroundTaskPool) -> None:
        self._pool = pool

    def get(self, reference: LocalBackgroundTaskReference) -> AsyncWorkQueryResult:
        local = reference.reference
        if local.owner.process_instance_id != self._pool.owner.process_instance_id:
            return AsyncWorkQueryResult(AsyncWorkQueryDisposition.OWNER_LOST, None)
        if local.owner != self._pool.owner:
            return AsyncWorkQueryResult(AsyncWorkQueryDisposition.INCARNATION_LOST, None)
        meta = self._pool.get_task_info(str(local.task_id))
        if meta is None or meta.attempt_id != local.attempt_id:
            return AsyncWorkQueryResult(AsyncWorkQueryDisposition.NOT_FOUND, None)
        pointer = None
        status = meta.status
        if status is BackgroundTaskStatus.SUCCESS:
            pointer = CompletedInlineTaskResultPointer(
                local.task_id,
                CommandName(meta.command_name),
                f"{meta.command_name} finished ({status.value}).",
                InlineTaskOutput(meta.result or ""),
            )
        elif status in TERMINAL_STATUSES:
            pointer = FailedTaskResultPointer(
                local.task_id,
                CommandName(meta.command_name),
                f"{meta.command_name} finished ({status.value}).",
                TaskFailure(meta.result or status.value),
            )
        pins = self._pool.pin_snapshot(owner=self._pool.owner)
        return AsyncWorkQueryResult(
            AsyncWorkQueryDisposition.FOUND,
            LocalBackgroundTaskObservation(
                reference,
                project_background_task_phase(status),
                LocalBackgroundObservationDetail(
                    label=meta.command_name,
                    owner_available=True,
                    pinned=local in pins.references,
                ),
                pointer,
                _actions(status),
            ),
        )

    def cancel(self, command: CancelLocalBackgroundTask) -> LocalCancelReceipt:
        reference = command.reference
        local = reference.reference
        if local.owner.process_instance_id != self._pool.owner.process_instance_id:
            disposition = LocalCancelDisposition.OWNER_LOST
        elif local.owner != self._pool.owner:
            disposition = LocalCancelDisposition.INCARNATION_LOST
        else:
            meta = self._pool.get_task_info(str(local.task_id))
            if meta is None:
                disposition = LocalCancelDisposition.NOT_FOUND
            elif meta.attempt_id != local.attempt_id:
                disposition = LocalCancelDisposition.STALE_ATTEMPT
            elif meta.status in TERMINAL_STATUSES:
                disposition = LocalCancelDisposition.ALREADY_TERMINAL
            elif self._pool.cancel(str(local.task_id)):
                disposition = LocalCancelDisposition.CANCEL_REQUESTED
            else:
                disposition = LocalCancelDisposition.NOT_FOUND
        return LocalCancelReceipt(reference, disposition)


__all__ = ["AgentOwnedLocalAsyncWorkAdapter", "project_background_task_phase"]
