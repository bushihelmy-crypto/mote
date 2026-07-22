"""Static/dynamic tool catalog activation and teardown lifecycle."""

from __future__ import annotations

from typing import Any

from mote.common.events import ToolsChangedEvent
from mote.common.logs import logger
from mote.executor.mcp_lifecycle import McpLifecycle
from mote.executor.tasks.bggraph.marker import is_pipeline_tool
from mote.executor.tool_catalog import ToolCatalog
from mote.executor.tool_registry import registry as tool_registry
from mote.executor.tool_settlement import ToolSettlement


class ToolLifecycle:
    def __init__(
        self,
        *,
        session_id: str,
        declared_tools: tuple[str, ...],
        role,
        pipelines_enabled: bool,
        catalog: ToolCatalog,
        mcp_lifecycle: McpLifecycle,
        settlement: ToolSettlement,
    ) -> None:
        self._session_id = session_id
        self._declared_tools = declared_tools
        self._role = role
        self._pipelines_enabled = pipelines_enabled
        self._catalog = catalog
        self._mcp_lifecycle = mcp_lifecycle
        self._settlement = settlement
        self._prepared = False
        self._preparing = False

    def prepare(self) -> None:
        if self._prepared or self._preparing:
            return
        self._preparing = True
        try:
            if self._declared_tools:
                tool_registry.discover()
                bound: dict[type, Any] = {}
                skipped: set[type] = set()
                for name in self._declared_tools:
                    tool_cls = tool_registry.get(name)
                    if tool_cls is None or tool_cls in skipped:
                        continue
                    if tool_cls not in bound:
                        instance = tool_cls()
                        instance.bind(self._session_id, role=self._role)
                        if not self._pipelines_enabled and is_pipeline_tool(instance):
                            skipped.add(tool_cls)
                            continue
                        bound[tool_cls] = instance
                    self._catalog.register(bound[tool_cls], tool_registry.all_names(tool_cls))
            self._prepared = True
        finally:
            self._preparing = False

    def register(self, tool: Any, names: list[str]) -> None:
        self.prepare()
        self._catalog.register(tool, names)

    async def deregister(self, name: str) -> bool:
        self.prepare()
        tool = self._catalog.get(name)
        if tool is None:
            return False
        removed = self._catalog.names_for(tool)
        self._catalog.remove(removed)
        self._cleanup_tool_session(tool, name)
        await self._announce(removed, f"ToolsChangedEvent for {name} not delivered")
        return True

    async def init_mcp(self, executor, mcps: list[str] | None, *, enabled: bool) -> None:
        if enabled and not self._mcp_lifecycle.active:
            await self._mcp_lifecycle.bind(mcps or None, executor)

    async def reload_mcp(self, executor, mcps: list[str] | None, *, enabled: bool) -> bool:
        if not enabled:
            return False
        self.prepare()
        removed = self._catalog.mcp_names()
        seen_ids: set[int] = set()
        for name in removed:
            tool = self._catalog.get(name)
            if id(tool) not in seen_ids:
                seen_ids.add(id(tool))
                self._cleanup_tool_session(tool, name)
        self._catalog.remove(removed)
        await self._mcp_lifecycle.teardown()
        await self._mcp_lifecycle.bind(mcps or None, executor)
        await self._announce(removed, "ToolsChangedEvent after MCP reload not delivered")
        return True

    async def _announce(self, removed: list[str], context: str) -> None:
        await self._settlement.observe(
            ToolsChangedEvent(
                removed=removed,
                reconstructable=sorted(self._catalog.reconstructable_names()),
            ),
            context=context,
        )

    def _cleanup_tool_session(self, tool: Any, name: str) -> None:
        try:
            tool.cleanup_session(self._session_id)
        except Exception as exc:
            logger.debug(f"ToolLifecycle: cleanup_session for {name} failed: {exc}")

    async def cleanup(self) -> None:
        seen_ids: set[int] = set()
        for tool in self._catalog.iter_unique():
            if id(tool) not in seen_ids:
                seen_ids.add(id(tool))
                self._cleanup_tool_session(tool, getattr(tool, "name", type(tool).__name__))
        self._catalog.clear()
        await self._mcp_lifecycle.teardown()

    @property
    def mcp(self):
        return self._mcp_lifecycle.mcp
