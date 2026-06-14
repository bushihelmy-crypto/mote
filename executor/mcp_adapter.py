"""MCPToolAdapter — wraps a discovered MCP tool as a BaseTool.

Lets MCP tools share the single dispatch path in ToolExecutor instead of a
separate fallback registry. Constructed at runtime (name/schema are only known
after MCP discovery), so it is NOT @register_tool'd — ToolExecutor places
instances directly into its _tools map.
"""
from __future__ import annotations

from typing import Any

from metagpt.executor.base_tool import BaseTool


class MCPToolAdapter(BaseTool):
    """Adapt one discovered MCP tool to the BaseTool interface."""

    def __init__(self, mcp, tool_name: str, schema: dict) -> None:
        super().__init__()
        self._mcp = mcp
        self.name = tool_name
        self._schema = schema

    async def call(self, **kwargs: Any) -> str:
        """Invoke the underlying MCP tool with LLM-specified parameters."""
        return await self._mcp.call_tool(self.name, kwargs)

    def tool_schema(self) -> dict:
        """Return the schema discovered from the MCP server."""
        return self._schema

    def native_schema(self) -> dict:
        """Return a native tool-use schema using the MCP-provided input schema.

        MCP servers already publish a JSON Schema (inputSchema), stored here as
        ``_schema["parameters"]``, so no signature inspection is needed — pass
        it straight through as ``input_schema``.
        """
        return {
            "name": self._schema.get("name", self.name),
            "description": self._schema.get("description", ""),
            "input_schema": self._schema.get("parameters") or {"type": "object", "properties": {}},
        }

