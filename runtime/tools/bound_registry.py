"""Pinned executable tool bindings keyed by immutable snapshot revision."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.tool.catalog import ToolDispatchRequest
from mote.runtime.tools.tool_binding import ExecutableToolBinding


@dataclass(frozen=True, slots=True)
class PinnedToolInvocation:
    semantic_identity: str
    canonical_name: str
    binding: ExecutableToolBinding
    catalog_generation: int


class BoundToolRegistry:
    def __init__(self) -> None:
        self._revisions: dict[tuple[str, int], dict[str, PinnedToolInvocation]] = {}

    def pin(self, snapshot_id: str, revision: int, tools: dict[str, PinnedToolInvocation]) -> None:
        key = (snapshot_id, revision)
        if key in self._revisions:
            raise ValueError("tool snapshot revision is already pinned")
        self._revisions[key] = dict(tools)

    def resolve(self, request: ToolDispatchRequest) -> tuple[PinnedToolInvocation | None, str]:
        tools = self._revisions.get((request.snapshot_id, request.registry_revision))
        if tools is None:
            return None, "unrecoverable_binding"
        tool = tools.get(request.tool_name)
        if tool is None:
            return None, "tool_not_in_snapshot"
        return tool, ""

    def release(self, snapshot_id: str, revision: int, *, references: int) -> bool:
        if references:
            return False
        return self._revisions.pop((snapshot_id, revision), None) is not None


__all__ = ["BoundToolRegistry", "PinnedToolInvocation"]
