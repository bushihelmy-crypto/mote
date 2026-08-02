"""Lifecycle owner for one executor's hot-reloadable MCP connection set."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mote.runtime.config.mcp import MCPServerConfig
from mote.runtime.tools.mcp.toolsets import NativeMcpToolset, XmlMcpToolset
from mote.runtime.tools.mcp.universal import UniversalMCP
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition


class McpLifecycle:
    """Own a shared MCP manager and one protocol-explicit definition projection."""

    def __init__(
        self,
        *,
        servers: list[MCPServerConfig] | None = None,
        oauth_root=None,
    ) -> None:
        self._mcp: UniversalMCP | None = None
        self._toolset: XmlMcpToolset | NativeMcpToolset | None = None
        self._servers = list(servers or [])
        self._oauth_root = oauth_root

    @property
    def mcp(self) -> UniversalMCP | None:
        return self._mcp

    @property
    def active(self) -> bool:
        return self._mcp is not None

    async def prepare_xml(self, mcps: list[str] | None) -> "McpCandidate":
        owner = await self._connect(mcps)
        try:
            toolset = XmlMcpToolset(owner)
            definitions = tuple(toolset.definitions())
            capabilities = tuple(definition.capability_factory() for definition in definitions)
        except BaseException:
            await owner.cleanup_clients()
            raise
        return McpCandidate(owner, toolset, tuple(zip(definitions, capabilities)))

    async def prepare_native(self, mcps: list[str] | None) -> "McpCandidate":
        owner = await self._connect(mcps)
        try:
            toolset = NativeMcpToolset(owner)
            definitions = tuple(toolset.definitions())
            capabilities = tuple(definition.capability_factory() for definition in definitions)
        except BaseException:
            await owner.cleanup_clients()
            raise
        return McpCandidate(owner, toolset, tuple(zip(definitions, capabilities)))

    def activate(self, candidate: "McpCandidate") -> UniversalMCP | None:
        previous = self._mcp
        self._mcp = candidate.owner
        self._toolset = candidate.toolset
        return previous

    @staticmethod
    async def discard(candidate: "McpCandidate") -> None:
        await candidate.owner.cleanup_clients()

    @staticmethod
    async def cleanup_owner(owner: UniversalMCP | None) -> None:
        if owner is not None:
            await owner.cleanup_clients()

    async def _connect(self, mcps: list[str] | None) -> UniversalMCP:
        owner = UniversalMCP(
            servers=self._servers,
            oauth_root=self._oauth_root,
        )
        await owner.initialize(server_names=mcps)
        return owner

    async def teardown(self) -> None:
        if self._mcp is not None:
            await self._mcp.cleanup_clients()
        self._mcp = None
        self._toolset = None


@dataclass(frozen=True, slots=True)
class McpCandidate:
    owner: UniversalMCP
    toolset: XmlMcpToolset | NativeMcpToolset
    bindings: tuple[tuple[XmlToolDefinition[Any] | NativeToolDefinition[Any], Any], ...]


__all__ = ["McpCandidate", "McpLifecycle"]
