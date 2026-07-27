"""MCPToolAdapter — wraps a discovered MCP tool as a BaseTool.

Lets MCP tools share the single dispatch path in ToolExecutor instead of a
separate fallback registry. Constructed at runtime (name/schema are only known
after MCP discovery), so it is NOT @register_tool'd — ToolExecutor places
instances directly into its _tools map.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from mote.contracts.tools import NativeToolSchema, XmlToolSchema
from mote.kernel.tools.definitions import NativeToolDefinition, XmlToolDefinition
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.mcp.types import DiscoveredMcpTool, McpToolCaller


class McpXmlSchemaError(ValueError):
    """An MCP input schema cannot be represented by the XML command channel."""


def _xml_scalar_type(tool_name: str, parameter: str, schema: dict[str, Any]) -> str:
    declared = schema.get("type", "string")
    if isinstance(declared, list):
        non_null = [value for value in declared if value != "null"]
        if len(non_null) != 1:
            raise McpXmlSchemaError(
                f"MCP tool {tool_name!r} parameter {parameter!r} has an XML-incompatible type union"
            )
        declared = non_null[0]
    if declared not in {"string", "integer", "number", "boolean"}:
        raise McpXmlSchemaError(
            f"MCP tool {tool_name!r} parameter {parameter!r} has XML-incompatible type {declared!r}"
        )
    for union_keyword in ("anyOf", "oneOf", "allOf"):
        if union_keyword in schema:
            raise McpXmlSchemaError(
                f"MCP tool {tool_name!r} parameter {parameter!r} uses XML-incompatible {union_keyword}"
            )
    return declared


def _xml_mcp_decoder(tool_name: str, input_schema: dict[str, Any]):
    if input_schema.get("type", "object") != "object":
        raise McpXmlSchemaError(f"MCP tool {tool_name!r} input schema must be an object")
    properties = input_schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise McpXmlSchemaError(f"MCP tool {tool_name!r} properties must be an object")
    scalar_types = {
        name: _xml_scalar_type(tool_name, name, schema)
        for name, schema in properties.items()
        if isinstance(schema, dict)
    }
    if len(scalar_types) != len(properties):
        raise McpXmlSchemaError(f"MCP tool {tool_name!r} contains an invalid parameter schema")

    def decode(arguments: dict[str, Any]) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for name, value in arguments.items():
            scalar_type = scalar_types.get(name)
            if scalar_type is None or not isinstance(value, str):
                decoded[name] = value
            elif scalar_type == "integer":
                decoded[name] = int(value)
            elif scalar_type == "number":
                decoded[name] = float(value)
            elif scalar_type == "boolean":
                normalized = value.strip().lower()
                if normalized not in {"true", "false"}:
                    raise ValueError(f"{tool_name}.{name} expects 'true' or 'false'")
                decoded[name] = normalized == "true"
            else:
                decoded[name] = value
        return decoded

    return decode


class MCPToolAdapter(BaseTool):
    """Adapt one discovered MCP tool to the BaseTool interface."""

    @classmethod
    def from_discovery(cls, mcp: McpToolCaller, tool: DiscoveredMcpTool) -> "MCPToolAdapter":
        """Create a capability type whose class identity matches the discovery."""

        type_stem = re.sub(r"\W+", "_", tool.name).strip("_") or "Anonymous"
        adapter_type = type(
            f"{type_stem}McpToolCapability",
            (cls,),
            {"name": tool.name},
        )
        return adapter_type(mcp, tool)

    def __init__(self, mcp: McpToolCaller, tool: DiscoveredMcpTool) -> None:
        super().__init__()
        if not self.name:
            raise TypeError("MCPToolAdapter must be created with from_discovery()")
        self._mcp = mcp
        self._tool = tool
        self._input_schema = deepcopy(tool.input_schema) or {"type": "object", "properties": {}}

    async def call(self, **kwargs: Any) -> str:
        """Invoke the underlying MCP tool with LLM-specified parameters."""
        return await self._mcp.call_tool(self.name, kwargs)

    def xml_definition(self) -> XmlToolDefinition[Any]:
        """Build the explicit XML registration with scalar argument decoding."""

        description = self._tool.description
        input_schema = self._input_schema
        decoder = _xml_mcp_decoder(self.name, input_schema)

        def render(_capability: Any) -> XmlToolSchema:
            return {
                "name": self.name,
                "description": description,
                "parameters": deepcopy(input_schema),
            }

        summary = description.splitlines()[0].strip() if description else self.name
        return XmlToolDefinition(
            name=self.name,
            aliases=self._tool.aliases,
            capability_factory=lambda: self,
            capability_type=type(self),
            schema_renderer=render,
            argument_decoder=decoder,
            description=description,
            summary=summary,
            search_text=summary,
            category="mcp",
        )

    def native_definition(self) -> NativeToolDefinition[Any]:
        """Build the explicit Native registration discovered from MCP."""

        description = self._tool.description

        def render(_capability: Any) -> NativeToolSchema:
            return {
                "name": self.name,
                "description": description,
                "input_schema": deepcopy(self._input_schema),
            }

        summary = description.splitlines()[0].strip() if description else self.name
        return NativeToolDefinition(
            name=self.name,
            aliases=self._tool.aliases,
            capability_factory=lambda: self,
            capability_type=type(self),
            schema_renderer=render,
            description=description,
            summary=summary,
            search_text=summary,
            category="mcp",
        )


__all__ = ["MCPToolAdapter", "McpXmlSchemaError"]
