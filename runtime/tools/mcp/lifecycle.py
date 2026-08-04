"""Lifecycle owner for one executor's hot-reloadable MCP connection set."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mote.runtime.config.mcp import MCPServerConfig
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.mcp.toolsets import NativeMcpToolset, XmlMcpToolset
from mote.runtime.tools.mcp.universal import UniversalMCP
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition


class McpLifecycleState(StrEnum):
    EMPTY = "empty"
    ACTIVE = "active"
    DRAINING = "draining"


class McpCleanupDisposition(StrEnum):
    SETTLED = "settled"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True, slots=True)
class McpCleanupReceipt:
    generation: int
    disposition: McpCleanupDisposition
    detail: str = ""


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
        self._generation = 0
        self._state = McpLifecycleState.EMPTY

    @property
    def mcp(self) -> UniversalMCP | None:
        return self._mcp

    @property
    def active(self) -> bool:
        return self._state is McpLifecycleState.ACTIVE and self._mcp is not None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def state(self) -> McpLifecycleState:
        return self._state

    async def prepare_xml(self, mcps: list[str] | None) -> "McpCandidate":
        owner = await self._connect(mcps)
        try:
            toolset = XmlMcpToolset(owner)
            definitions = tuple(toolset.definitions())
            capabilities = tuple(definition.capability_factory() for definition in definitions)
        except BaseException:
            await owner.cleanup_clients()
            raise
        return McpCandidate(self._generation + 1, owner, toolset, tuple(zip(definitions, capabilities)))

    async def prepare_native(self, mcps: list[str] | None) -> "McpCandidate":
        owner = await self._connect(mcps)
        try:
            toolset = NativeMcpToolset(owner)
            definitions = tuple(toolset.definitions())
            capabilities = tuple(definition.capability_factory() for definition in definitions)
        except BaseException:
            await owner.cleanup_clients()
            raise
        return McpCandidate(self._generation + 1, owner, toolset, tuple(zip(definitions, capabilities)))

    def activate(self, candidate: "McpCandidate") -> UniversalMCP | None:
        if self._state is McpLifecycleState.DRAINING:
            raise RuntimeError("MCP lifecycle is draining a failed generation")
        if candidate.generation != self._generation + 1:
            raise RuntimeError("MCP candidate generation is stale")
        previous = self._mcp
        self._mcp = candidate.owner
        self._toolset = candidate.toolset
        self._generation = candidate.generation
        self._state = McpLifecycleState.ACTIVE
        return previous

    async def settle_prior(self, owner: UniversalMCP | None, *, generation: int) -> McpCleanupReceipt:
        try:
            await self.cleanup_owner(owner)
        except Exception as exc:
            self._state = McpLifecycleState.DRAINING
            return McpCleanupReceipt(
                generation,
                McpCleanupDisposition.CLEANUP_FAILED,
                f"{type(exc).__name__}: {exc}",
            )
        if self._state is McpLifecycleState.DRAINING:
            self._state = McpLifecycleState.ACTIVE if self._mcp is not None else McpLifecycleState.EMPTY
        return McpCleanupReceipt(generation, McpCleanupDisposition.SETTLED)

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
        owner = self._mcp
        generation = self._generation
        self._state = McpLifecycleState.DRAINING
        receipt = await self.settle_prior(owner, generation=generation)
        if receipt.disposition is McpCleanupDisposition.CLEANUP_FAILED:
            raise RuntimeError(f"MCP generation cleanup failed: {receipt.detail}")
        self._mcp = None
        self._toolset = None
        self._state = McpLifecycleState.EMPTY


@dataclass(frozen=True, slots=True)
class McpCandidate:
    generation: int
    owner: UniversalMCP
    toolset: XmlMcpToolset | NativeMcpToolset
    bindings: tuple[tuple[XmlToolDefinition | NativeToolDefinition, BaseTool], ...]


__all__ = [
    "McpCandidate",
    "McpCleanupDisposition",
    "McpCleanupReceipt",
    "McpLifecycle",
    "McpLifecycleState",
]
