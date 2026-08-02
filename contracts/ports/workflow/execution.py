"""Consumer-owned Port for compiling and executing declarative Workflow nodes."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.workflow.execution import WorkflowNodeDispatchResult


class WorkflowNodeExecutionPort(Protocol):
    async def dispatch(self, tool_name: str, arguments: dict[str, object]) -> WorkflowNodeDispatchResult: ...

    def allowed_tool_names(self) -> tuple[str, ...]: ...


__all__ = ["WorkflowNodeExecutionPort"]
