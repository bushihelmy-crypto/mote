"""Lifecycle owner for one executor's hot-reloadable MCP connection set."""

from __future__ import annotations

from typing import Any, Protocol

from mote.kernel.tools.definitions import NativeToolDefinition, XmlToolDefinition
from mote.runtime.tools.mcp.toolsets import NativeMcpToolset, XmlMcpToolset
from mote.runtime.tools.mcp.universal import UniversalMCP


class XmlMcpRegistrar(Protocol):
    """Registration boundary accepted by :meth:`McpLifecycle.bind_xml`."""

    def register_xml_tool(self, definition: XmlToolDefinition[Any], capability: Any) -> None:
        ...


class NativeMcpRegistrar(Protocol):
    """Registration boundary accepted by :meth:`McpLifecycle.bind_native`."""

    def register_native_tool(self, definition: NativeToolDefinition[Any], capability: Any) -> None:
        ...


class McpLifecycle:
    """Own a shared MCP manager and one protocol-explicit definition projection."""

    def __init__(self) -> None:
        self._mcp: UniversalMCP | None = None
        self._toolset: XmlMcpToolset | NativeMcpToolset | None = None

    @property
    def mcp(self) -> UniversalMCP | None:
        return self._mcp

    @property
    def active(self) -> bool:
        return self._mcp is not None

    async def bind_xml(self, mcps: list[str] | None, registrar: XmlMcpRegistrar) -> None:
        """Discover MCP capabilities and install only their XML definitions."""

        owner = await self._connect(mcps)
        try:
            toolset = XmlMcpToolset(owner)
            for definition in toolset.definitions():
                registrar.register_xml_tool(definition, definition.capability_factory())
        except BaseException:
            await owner.cleanup_clients()
            raise
        self._mcp = owner
        self._toolset = toolset

    async def bind_native(self, mcps: list[str] | None, registrar: NativeMcpRegistrar) -> None:
        """Discover MCP capabilities and install only their Native definitions."""

        owner = await self._connect(mcps)
        try:
            toolset = NativeMcpToolset(owner)
            for definition in toolset.definitions():
                registrar.register_native_tool(definition, definition.capability_factory())
        except BaseException:
            await owner.cleanup_clients()
            raise
        self._mcp = owner
        self._toolset = toolset

    @staticmethod
    async def _connect(mcps: list[str] | None) -> UniversalMCP:
        owner = UniversalMCP()
        await owner.initialize(server_names=mcps)
        return owner

    async def teardown(self) -> None:
        if self._mcp is not None:
            await self._mcp.cleanup_clients()
        self._mcp = None
        self._toolset = None


__all__ = ["McpLifecycle", "NativeMcpRegistrar", "XmlMcpRegistrar"]
