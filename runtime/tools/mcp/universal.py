import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List

from fastmcp import Client  # type: ignore[reportMissingImports]

from mote.contracts.config.mcp import MCPServerConfig, MCPTransportType
from mote.runtime.errors import ToolNotFoundError
from mote.runtime.logging import logger
from mote.runtime.tools.mcp.config_source import load_mcp_servers
from mote.runtime.tools.mcp.oauth import build_mcp_auth
from mote.runtime.tools.mcp.types import DiscoveredMcpTool

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the return site).
_MSG_MCP_READY = "MCP tools available ({count}): {tools}"
_MSG_MCP_FAILED = "MCP tool discovery failed: {errors}"
_MSG_MCP_NONE = "No external MCP tools configured."


class MCPInitState(str, Enum):
    UNCONFIGURED = "unconfigured"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _McpToolEntry:
    discovered: DiscoveredMcpTool
    server_config: MCPServerConfig


class UniversalMCP:
    """Universal MCP manager that discovers tools from configured MCP servers."""

    def __init__(self):
        self._tool_registry: dict[str, _McpToolEntry] = {}
        self.clients: Dict[str, Client] = {}
        self.initialized_servers: Dict[str, Dict[str, Any]] = {}
        self.initialization_errors: Dict[str, str] = {}
        self.state: MCPInitState = MCPInitState.UNCONFIGURED

    async def initialize(
        self, server_names: List[str] | None = None, servers: List[MCPServerConfig] | None = None
    ) -> None:
        """Connect to configured MCP servers and discover tools.

        Args:
            server_names: Only initialize servers with these names (from Role.mcps).
                         If None, initializes all enabled servers.
            servers: Explicit server configs. Overrides server_names if provided.
        """
        await self.cleanup_clients()
        self._tool_registry.clear()
        self.initialized_servers.clear()
        self.initialization_errors.clear()

        if servers is None:
            # MCP servers are defined in their own ``mcp_config.json`` (the
            # de-facto MCP shape), not the layered ``config.yaml``. Every
            # entry present there is enabled (presence == enabled).
            all_servers = load_mcp_servers()
            if server_names is not None:
                servers = [s for s in all_servers if s.name in server_names]
            else:
                servers = all_servers

        if not servers:
            self.state = MCPInitState.UNCONFIGURED
            return

        for server_config in servers:
            server_name = server_config.name
            identifier = server_config.url or server_config.command or server_name

            try:
                client = self._build_client(server_config)
                async with client:
                    tools = await client.list_tools()

                    for tool in tools:
                        namespaced_name = f"{server_name}:{tool.name}"
                        # Resolve aliases from config
                        aliases = server_config.aliases.get(tool.name, [])
                        self._tool_registry[namespaced_name] = _McpToolEntry(
                            discovered=DiscoveredMcpTool(
                                name=namespaced_name,
                                description=tool.description or "",
                                input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                                aliases=tuple(aliases),
                            ),
                            server_config=server_config,
                        )

                    self.initialized_servers[server_name] = {
                        "transport": server_config.type.value,
                        "tool_count": len(tools),
                        "identifier": identifier,
                    }
            except Exception as e:
                logger.exception(f"Failed to initialize tools from {server_name}: {e}")
                self.initialization_errors[server_name] = str(e)

        self.state = (
            MCPInitState.READY
            if self._tool_registry
            else MCPInitState.FAILED
            if self.initialization_errors
            else MCPInitState.UNCONFIGURED
        )

    async def call_tool(self, tool_name: str, parameters: Dict[str, Any]) -> str:
        """Call a tool by name, return JSON string result."""
        if tool_name not in self._tool_registry:
            raise ToolNotFoundError(f"Tool '{tool_name}' not found. Available: {list(self._tool_registry.keys())}")

        server_name, original_tool_name = tool_name.split(":", 1)
        server_config = self._tool_registry[tool_name].server_config

        client = self.clients.get(server_name)
        if not client:
            client = self._build_client(server_config)
            await client.__aenter__()
            self.clients[server_name] = client

        try:
            result = await client.call_tool(original_tool_name, parameters)
        except Exception:
            await self._close_client(server_name)
            raise

        processed = []
        for content in result.content or []:
            content_type = getattr(content, "type", None)
            if content_type == "text":
                processed.append(content.model_dump() if hasattr(content, "model_dump") else str(content))
            else:
                processed.append(f"'{content_type}' is not currently supported")

        return json.dumps({"mcp_tool_result": processed or None}, indent=2, ensure_ascii=False)

    def discovered_tools(self) -> tuple[DiscoveredMcpTool, ...]:
        """Return the protocol-neutral discovery snapshot."""

        return tuple(entry.discovered for entry in self._tool_registry.values())

    def get_status_description(self) -> str:
        """Human-readable status for system prompt."""
        if self.state == MCPInitState.READY:
            return _MSG_MCP_READY.format(count=len(self._tool_registry), tools=list(self._tool_registry.keys()))
        elif self.state == MCPInitState.FAILED:
            return _MSG_MCP_FAILED.format(errors=self.initialization_errors)
        return _MSG_MCP_NONE

    async def cleanup_clients(self) -> None:
        failures: list[tuple[str, BaseException]] = []
        for server_name, client in list(self.clients.items()):
            try:
                await client.__aexit__(None, None, None)
            except Exception as exc:
                failures.append((server_name, exc))
            else:
                self.clients.pop(server_name, None)
        if failures:
            details = "; ".join(f"{server_name}: {type(exc).__name__}: {exc}" for server_name, exc in failures)
            raise RuntimeError(f"MCP client shutdown failed: {details}")

    async def _close_client(self, server_name: str) -> None:
        client = self.clients.get(server_name)
        if client:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass
            else:
                self.clients.pop(server_name, None)

    def _build_client(self, server_config: MCPServerConfig) -> Client:
        if server_config.type == MCPTransportType.SSE:
            assert server_config.url is not None, "SSE server config requires a url"
            # A remote server may require an OAuth bearer; STDIO (local process)
            # has no HTTP auth surface, so auth is SSE-only. build_mcp_auth
            # returns None when no oauth is configured (unauthenticated client).
            return Client(server_config.url, auth=build_mcp_auth(server_config))
        return Client(
            {
                "mcpServers": {
                    server_config.name: {
                        "command": server_config.command,
                        "args": server_config.args or [],
                        "env": server_config.env or {},
                    }
                }
            }
        )
