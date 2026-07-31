"""Protocol-explicit Toolsets projected from one initialized MCP owner."""

from __future__ import annotations

from mote.runtime.tools.mcp.adapter import MCPToolAdapter
from mote.runtime.tools.mcp.types import McpDiscoverySource
from mote.runtime.tools.provider import NativeToolset, XmlToolset


class XmlMcpToolset(XmlToolset):
    """MCP capabilities explicitly adapted to the XML command protocol."""

    def __init__(self, source: McpDiscoverySource, *, version: str = "1") -> None:
        capabilities = tuple(MCPToolAdapter.from_discovery(source, tool) for tool in source.discovered_tools())
        self._capabilities = capabilities
        super().__init__(
            "mcp:xml",
            tuple(capability.xml_definition() for capability in capabilities),
            version=version,
        )


class NativeMcpToolset(NativeToolset):
    """MCP capabilities explicitly adapted to provider-native tool use."""

    def __init__(self, source: McpDiscoverySource, *, version: str = "1") -> None:
        capabilities = tuple(MCPToolAdapter.from_discovery(source, tool) for tool in source.discovered_tools())
        self._capabilities = capabilities
        super().__init__(
            "mcp:native",
            tuple(capability.native_definition() for capability in capabilities),
            version=version,
        )


__all__ = ["NativeMcpToolset", "XmlMcpToolset"]
