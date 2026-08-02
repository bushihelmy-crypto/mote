"""Typed MCP discovery and invocation boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class DiscoveredMcpTool:
    """One protocol-neutral capability discovered from an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]
    source_identity: str
    aliases: tuple[str, ...] = ()


class McpToolCaller(Protocol):
    """Narrow capability used by an MCP tool adapter at execution time."""

    async def call_tool(self, tool_name: str, parameters: dict[str, Any]) -> str: ...


class McpDiscoverySource(McpToolCaller, Protocol):
    """Initialized MCP owner that exposes a discovery snapshot."""

    def discovered_tools(self) -> tuple[DiscoveredMcpTool, ...]: ...


__all__ = ["DiscoveredMcpTool", "McpDiscoverySource", "McpToolCaller"]
