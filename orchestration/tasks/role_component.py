"""Orchestration-owned background-task component for a Runtime Agent."""

from __future__ import annotations

from mote.orchestration.tasks.disk_output import TaskOutputStore
from mote.orchestration.tasks.pool import BackgroundTaskPool
from mote.orchestration.tasks.status import PAUSE_STATUSES, TERMINAL_STATUSES, BgStatus
from mote.runtime.resources import build_task_result_pointer


def build_background_task_pool(ctx) -> BackgroundTaskPool:
    """Build and wire the task pool through Runtime's narrow component context."""
    role = ctx.role
    output_store = TaskOutputStore(session_id=role.state.session_id, store=ctx.dep("workspace_store"))
    pool = BackgroundTaskPool(
        msg_buffer=role.state.msg_buffer,
        output_store=output_store,
        wake=ctx.state.pending_task_completion_wake,
        session_id=role.state.session_id,
    )

    output_store.set_on_cap(pool.cancel_for_cap)

    def on_terminal(meta) -> None:
        status_value = meta.status.value if isinstance(meta.status, BgStatus) else str(meta.status)
        if meta.status in PAUSE_STATUSES:
            content = build_task_result_pointer(
                task_id=meta.task_id,
                command_name=meta.command_name,
                status=status_value,
                summary=f"{meta.command_name} paused ({status_value}), awaiting a decision.",
            )
        elif meta.status in TERMINAL_STATUSES:
            content = build_task_result_pointer(
                task_id=meta.task_id,
                command_name=meta.command_name,
                status=status_value,
                summary=f"{meta.command_name} finished ({status_value}).",
                result=meta.result,
                output_path=meta.output_path,
            )
        else:
            return
        role._capabilities.register_task_result(meta.task_id, content)
        meta.registered_resource = True

    pool.set_on_terminal_result(on_terminal)
    pool.set_retire_result(role.resource_registry.unload)
    return pool


__all__ = ["build_background_task_pool"]
