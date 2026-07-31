"""Coding-Agent composition adapter for background task orchestration."""

from __future__ import annotations

from mote.contracts.ports.task.operations import BackgroundTaskBuildContext, BackgroundWakeReason
from mote.contracts.task.models import (
    CommandName,
    CompletedInlineTaskResultPointer,
    FailedTaskResultPointer,
    InlineTaskOutput,
    PausedTaskResultPointer,
    PauseReason,
    TaskFailure,
    TaskId,
)
from mote.orchestration.background_tasks import BackgroundTaskPool
from mote.orchestration.background_tasks.result_pointer import render_task_result_pointer
from mote.orchestration.background_tasks.results.store import TaskOutputStore
from mote.orchestration.background_tasks.status import PAUSE_STATUSES, TERMINAL_STATUSES, BgStatus
from mote.orchestration.workflows import WorkflowRun
from mote.orchestration.workflows.definition import WorkflowDefinition
from mote.product.workflows import WorkflowContinuationRegistry, WorkflowInspectionPort, WorkflowTaskAdapter


class AgentBackgroundTasks:
    """Product composition of generic background and Workflow services."""

    def __init__(self, pool: BackgroundTaskPool, session_id: str) -> None:
        self._pool = pool
        self._continuations = WorkflowContinuationRegistry(session_id)
        self._inspection = WorkflowInspectionPort()

    def submit(
        self,
        operation,
        command_name: str,
        *,
        graph_meta=None,
        max_restarts: int = 3,
        **options,
    ) -> str:
        if graph_meta is None:
            return self._pool.submit(
                operation,
                command_name,
                **options,
            )
        run = self._workflow_run(graph_meta)
        task_id = self._pool.submit(
            WorkflowTaskAdapter(run, self._continuations),
            command_name,
            **options,
        )
        self._inspection.register(
            task_id,
            run,
            graph_meta,
            max_restarts=max_restarts,
        )
        return task_id

    def submit_workflow(
        self,
        run: WorkflowRun,
        command_name: str,
        *,
        timeout: float | None = None,
    ) -> str:
        operation = WorkflowTaskAdapter(run, self._continuations)
        task_id = self._pool.submit(operation, command_name, timeout=timeout)
        self._inspection.register(task_id, run)
        return task_id

    def resume_workflow(
        self,
        task_id: str,
        resume_ref: str,
        overrides: dict | None = None,
        *,
        from_nodes: tuple[str, ...] = (),
        skip_nodes: tuple[str, ...] = (),
    ) -> str:
        run = self._continuations.consume(
            resume_ref,
            overrides,
            from_nodes=from_nodes,
            skip_nodes=skip_nodes,
        )
        operation = WorkflowTaskAdapter(run, self._continuations)
        self._inspection.register(task_id, run)
        return self._pool.resubmit(task_id, operation)

    def workflow_snapshot(self, task_id: str):
        return self._inspection.snapshot(task_id)

    def get_task_info(self, task_id: str):
        meta = self._pool.get_task_info(task_id)
        return self._inspection.view(task_id, meta) or meta

    def get_run_state(self, task_id: str):
        view = self._inspection.view(task_id)
        return view.run_state if view is not None else None

    def resubmit(
        self,
        task_id: str,
        operation,
        *,
        graph_meta=None,
        **options,
    ) -> str:
        if graph_meta is None:
            result = self._pool.resubmit(task_id, operation, **options)
            self._inspection.record_retry(task_id)
            return result
        run = self._workflow_run(graph_meta)
        result = self._pool.resubmit(
            task_id,
            WorkflowTaskAdapter(run, self._continuations),
            **options,
        )
        self._inspection.register(task_id, run, graph_meta)
        return result

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

    def mark_retrieved(self, task_id: str) -> None:
        self._pool.mark_retrieved(task_id)
        if self._pool.get_task_info(task_id) is None:
            self._inspection.discard(task_id)

    def cancel(self, task_id: str) -> bool:
        return self._pool.cancel(task_id)

    @staticmethod
    def _workflow_run(graph_meta) -> WorkflowRun:
        graph = graph_meta.graph_ref
        if graph is None:
            raise ValueError("workflow metadata requires a graph")
        graph._prepare()
        definition = WorkflowDefinition(
            definition_id=f"workflow:{graph.command_name}",
            version=int(getattr(graph, "definition_version", 1)),
            _graph=graph,
        )
        initial = dict(graph_meta.initial_params or {})
        return definition.start(
            initial,
            checkpoint=graph_meta.state,
            run_state=graph_meta.run_state,
            from_nodes=tuple(graph_meta.from_nodes),
            skip_nodes=tuple(graph_meta.skip_nodes),
        )

    def get_outcome(self, task_id: str):
        return self._pool.get_outcome(task_id)

    async def aclose(self) -> None:
        await self._pool.aclose()
        await self._continuations.aclose()
        await self._inspection.aclose()


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
    )

    def on_cap(task_id: str) -> None:
        pool.cancel_for_cap(task_id)

    output_store.set_on_cap(on_cap)

    def on_terminal(meta) -> None:
        status_value = meta.status.value if isinstance(meta.status, BgStatus) else str(meta.status)
        if meta.status in PAUSE_STATUSES:
            pointer = PausedTaskResultPointer(
                task_id=TaskId(meta.task_id),
                command_name=CommandName(meta.command_name),
                summary=f"{meta.command_name} paused ({status_value}), awaiting a decision.",
                reason=PauseReason(status_value),
            )
        elif meta.status == BgStatus.SUCCESS:
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
        context.result_registry.unload(task_id)

    pool.set_retire_result(retire)
    return AgentBackgroundTasks(pool, str(context.session_id))


__all__ = ["build_background_task_pool"]
