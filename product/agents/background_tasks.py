"""Coding-Agent composition adapter for background task orchestration."""

from __future__ import annotations

from mote.contracts.async_work.command import CancelLocalBackgroundTask, LocalCancelReceipt
from mote.contracts.async_work.identity import LocalBackgroundTaskReference
from mote.contracts.ports.task.operations import BackgroundMessageSink, BackgroundTaskBuildContext, BackgroundWakeReason
from mote.contracts.session.identity import SessionId
from mote.contracts.task.lifecycle import BackgroundTaskAcceptance, BackgroundTaskOwner, LocalTaskReference
from mote.contracts.task.models import (
    CommandName,
    CompletedInlineTaskResultPointer,
    FailedTaskResultPointer,
    InlineTaskOutput,
    TaskFailure,
    TaskId,
)
from mote.orchestration.background_tasks import BackgroundTaskPool
from mote.orchestration.background_tasks.observation import AgentOwnedLocalAsyncWorkAdapter
from mote.orchestration.background_tasks.result_pointer import render_task_result_pointer
from mote.orchestration.background_tasks.results.store import TaskOutputStore
from mote.orchestration.background_tasks.status import TERMINAL_STATUSES, BackgroundTaskStatus


class AgentBackgroundTasks:
    """Product adapter for one Agent-owned process-local BackgroundTaskPool."""

    def __init__(
        self,
        pool: BackgroundTaskPool,
        session_id: SessionId,
        message_sink: BackgroundMessageSink,
    ) -> None:
        self._pool = pool
        self._session_id = session_id
        self._message_sink = message_sink

    @property
    def session_id(self) -> SessionId:
        return self._session_id

    @property
    def message_sink(self) -> BackgroundMessageSink:
        return self._message_sink

    def async_work_adapter(self) -> AgentOwnedLocalAsyncWorkAdapter:
        return AgentOwnedLocalAsyncWorkAdapter(self._pool)

    def submit(
        self,
        operation,
        command_name: str,
        **options,
    ) -> BackgroundTaskAcceptance:
        return self._pool.submit(operation, command_name, **options)

    def get_task_info(self, task_id: str):
        meta = self._pool.get_task_info(task_id)
        return meta

    async def wait_all(self) -> None:
        await self._pool.wait_all()

    def has_pending(self) -> bool:
        return self._pool.has_pending()

    @property
    def pending_count(self) -> int:
        return self._pool.pending_count

    async def wait_any(self, timeout: float = 120.0) -> BackgroundWakeReason:
        return await self._pool.wait_any(timeout)

    async def wait_for_completion(self, timeout: float | None = None) -> bool:
        return await self._pool.wait_for_completion(timeout)

    def set_wake(self, wake) -> None:
        self._pool.set_wake(wake)

    @property
    def owner(self) -> BackgroundTaskOwner:
        return self._pool.owner

    def close_admission(self, *, owner: BackgroundTaskOwner):
        return self._pool.close_admission(owner=owner)

    def pin_snapshot(self, *, owner: BackgroundTaskOwner):
        return self._pool.pin_snapshot(owner=owner)

    async def drain(self, *, owner: BackgroundTaskOwner, timeout_seconds: float):
        receipt = await self._pool.drain(owner=owner, timeout_seconds=timeout_seconds)
        return receipt

    def mark_retrieved(self, task_id: str) -> None:
        self._pool.mark_retrieved(task_id)

    def cancel_current(self, task_id: str, reason: str) -> LocalCancelReceipt:
        meta = self._pool.get_task_info(task_id)
        if meta is None:
            raise KeyError(task_id)
        reference = LocalBackgroundTaskReference(
            LocalTaskReference(
                self._pool.owner,
                TaskId(meta.task_id),
                meta.attempt_id,
            )
        )
        return self.async_work_adapter().cancel(CancelLocalBackgroundTask(reference, reason))

    def get_outcome(self, task_id: str):
        return self._pool.get_outcome(task_id)

    async def aclose(self) -> None:
        await self._pool.aclose()


def build_background_task_pool(
    context: BackgroundTaskBuildContext,
) -> AgentBackgroundTasks:
    output_store = TaskOutputStore(
        session_id=context.session_id,
        store=context.output_locations,
    )
    pool = BackgroundTaskPool(
        msg_buffer=context.message_sink,
        output_store=output_store,
        wake=context.wake.wake,
        session_id=context.session_id,
        owner=context.owner,
    )

    def on_cap(task_id: str) -> None:
        pool.cancel_for_cap(task_id)

    output_store.set_on_cap(on_cap)

    def on_terminal(meta) -> None:
        status_value = meta.status.value if isinstance(meta.status, BackgroundTaskStatus) else str(meta.status)
        if meta.status == BackgroundTaskStatus.SUCCESS:
            pointer = CompletedInlineTaskResultPointer(
                task_id=TaskId(meta.task_id),
                command_name=CommandName(meta.command_name),
                summary=f"{meta.command_name} finished ({status_value}).",
                output=InlineTaskOutput(meta.result or ""),
            )
        elif meta.status in TERMINAL_STATUSES:
            pointer = FailedTaskResultPointer(
                task_id=TaskId(meta.task_id),
                command_name=CommandName(meta.command_name),
                summary=f"{meta.command_name} finished ({status_value}).",
                error=TaskFailure(meta.result or status_value),
            )
        else:
            return
        context.result_registry.register_task_result(TaskId(meta.task_id), render_task_result_pointer(pointer))
        meta.registered_resource = True

    pool.set_on_terminal_result(on_terminal)

    def retire(task_id: str) -> None:
        context.result_registry.unload(TaskId(task_id))

    pool.set_retire_result(retire)
    return AgentBackgroundTasks(
        pool,
        context.session_id,
        context.message_sink,
    )


__all__ = ["build_background_task_pool"]
