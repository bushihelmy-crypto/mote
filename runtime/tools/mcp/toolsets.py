"""Protocol-explicit Toolsets projected from one initialized MCP owner."""

from __future__ import annotations

from mote.runtime.tools.definition_compiler import compile_tool_catalog_identity, compile_tool_definition
from mote.runtime.tools.mcp.adapter import MCPToolAdapter
from mote.runtime.tools.mcp.types import McpDiscoverySource
from mote.runtime.tools.provider import NativeToolset, XmlToolset


class XmlMcpToolset(XmlToolset):
    """MCP capabilities explicitly adapted to the XML command protocol."""

    def __init__(self, source: McpDiscoverySource) -> None:
        capabilities = tuple(MCPToolAdapter.from_discovery(source, tool) for tool in source.discovered_tools())
        self._capabilities = capabilities
        definitions = tuple(capability.xml_definition() for capability in capabilities)
        version = compile_tool_catalog_identity(
            tuple(
                compile_tool_definition(definition, capability, approval_identity="none")
                for definition, capability in zip(definitions, capabilities, strict=True)
            )
        )
        super().__init__(
            "mcp:xml",
            definitions,
            version=version,
        )


class NativeMcpToolset(NativeToolset):
    """MCP capabilities explicitly adapted to provider-native tool use."""

    def __init__(self, source: McpDiscoverySource) -> None:
        capabilities = tuple(MCPToolAdapter.from_discovery(source, tool) for tool in source.discovered_tools())
        self._capabilities = capabilities
        definitions = tuple(capability.native_definition() for capability in capabilities)
        version = compile_tool_catalog_identity(
            tuple(
                compile_tool_definition(definition, capability, approval_identity="none")
                for definition, capability in zip(definitions, capabilities, strict=True)
            )
        )
        super().__init__(
            "mcp:native",
            definitions,
            version=version,
        )


__all__ = ["NativeMcpToolset", "XmlMcpToolset"]
