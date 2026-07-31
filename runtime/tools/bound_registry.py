"""Pinned executable tool bindings keyed by immutable snapshot revision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mote.contracts.tool.catalog import ToolDispatchRequest, ToolDispatchResult, ToolExecutionOutcome

BoundCallable = Callable[[dict[str, Any]], Awaitable[Any]]


class UnrecoverableBindingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BoundTool:
    semantic_identity: str
    invoke: BoundCallable


class BoundToolRegistry:
    def __init__(self) -> None:
        self._revisions: dict[tuple[str, int], dict[str, BoundTool]] = {}

    def pin(self, snapshot_id: str, revision: int, tools: dict[str, BoundTool]) -> None:
        key = (snapshot_id, revision)
        if key in self._revisions:
            raise ValueError("tool snapshot revision is already pinned")
        self._revisions[key] = dict(tools)

    async def dispatch(self, request: ToolDispatchRequest) -> ToolDispatchResult[ToolExecutionOutcome]:
        tools = self._revisions.get((request.snapshot_id, request.registry_revision))
        if tools is None:
            return ToolDispatchResult(False, conflict="unrecoverable_binding")
        tool = tools.get(request.tool_name)
        if tool is None:
            return ToolDispatchResult(False, conflict="tool_not_in_snapshot")
        arguments = dict(request.arguments)
        if request.call_id:
            arguments["__mote_call_id"] = request.call_id
        try:
            value = await tool.invoke(arguments)
        except UnrecoverableBindingError:
            return ToolDispatchResult(False, conflict="unrecoverable_binding")
        return ToolDispatchResult(True, value=value)

    def release(self, snapshot_id: str, revision: int, *, references: int) -> bool:
        if references:
            return False
        return self._revisions.pop((snapshot_id, revision), None) is not None


__all__ = ["BoundTool", "BoundToolRegistry", "UnrecoverableBindingError"]
