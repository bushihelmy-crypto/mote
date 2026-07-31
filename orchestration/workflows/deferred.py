"""Workflow-owned deferred execution descriptor."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Coroutine

from mote.runtime.tools.tool_result import ToolResult


class WorkflowExecutionMode(str, Enum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"
    HYBRID = "hybrid"


@dataclass
class WorkflowRunMetadata:
    graph_ref: Any = None
    initial_params: dict | None = None
    factory: Callable[..., Awaitable["WorkflowDeferredResult"]] | None = None
    run_state: Any = None
    state: Any = None
    from_nodes: tuple[str, ...] = ()
    skip_nodes: tuple[str, ...] = ()


@dataclass
class WorkflowDeferredResult:
    is_background_result = True

    mode: WorkflowExecutionMode = WorkflowExecutionMode.FOREGROUND
    result: Any = None
    poll_factory: Callable[[], Coroutine] | None = field(default=None, repr=False)
    command_name: str = ""
    graph_meta: WorkflowRunMetadata | None = field(default=None, repr=False)

    @classmethod
    def background(
        cls,
        poll_factory: Callable[[], Coroutine],
        *,
        command_name: str,
        graph_meta: WorkflowRunMetadata | None = None,
    ) -> "WorkflowDeferredResult":
        return cls(
            mode=WorkflowExecutionMode.BACKGROUND,
            poll_factory=poll_factory,
            command_name=command_name,
            graph_meta=graph_meta,
        )

    @classmethod
    def hybrid(
        cls,
        result: Any,
        poll_factory: Callable[[], Coroutine],
        *,
        command_name: str,
        graph_meta: WorkflowRunMetadata | None = None,
    ) -> "WorkflowDeferredResult":
        return cls(
            mode=WorkflowExecutionMode.HYBRID,
            result=result,
            poll_factory=poll_factory,
            command_name=command_name,
            graph_meta=graph_meta,
        )

    def to_tool_result(self, pool: Any, tool_name: str) -> ToolResult:
        task_id = None
        if self.poll_factory is not None and pool is not None:
            task_id = pool.submit(
                self.poll_factory,
                command_name=self.command_name or tool_name,
                graph_meta=self.graph_meta,
                progress=True,
            )
        if self.mode is WorkflowExecutionMode.BACKGROUND:
            task_ref = f" (task_id: {task_id})" if task_id is not None else ""
            output = (
                f"Background task '{self.command_name or tool_name}' submitted{task_ref}. "
                "Running asynchronously — you will be notified when it completes."
            )
            graph = self.graph_meta.graph_ref if self.graph_meta is not None else None
            summary = getattr(graph, "stage_summary", "") if graph is not None else ""
            if summary:
                output = f"{output}\nstage-summary:\n{summary}"
        else:
            output = str(self.result) if self.result is not None else ""
        return ToolResult(output=output, success=True, data=self)


BgTaskResult = WorkflowDeferredResult
GraphMeta = WorkflowRunMetadata

__all__ = [
    "WorkflowDeferredResult",
    "WorkflowExecutionMode",
    "WorkflowRunMetadata",
]
