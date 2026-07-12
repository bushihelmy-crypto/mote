import json
from enum import Enum
from typing import Any, Dict, List

from fastmcp import Client

from metagpt.common.config.config.mcp_config import MCPServerConfig, MCPTransportType
from metagpt.common.config.loader import load_config
from metagpt.common.exception import ToolNotFoundError
from metagpt.common.logs import logger
from metagpt.executor.mcp_adapter import MCPToolAdapter


class MCPInitState(str, Enum):
    UNCONFIGURED = "unconfigured"
    READY = "ready"
    FAILED = "failed"


class UniversalMCP:
    """Universal MCP manager that discovers tools from configured MCP servers."""

    def __init__(self):
        self.tool_registry: Dict[str, Dict[str, Any]] = {}
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
        self.tool_registry.clear()
        self.initialized_servers.clear()
        self.initialization_errors.clear()

        if servers is None:
            all_servers = [s for s in load_config().mcp.servers if s.enabled]
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
                        self.tool_registry[namespaced_name] = {
                            "name": namespaced_name,
                            "description": tool.description,
                            "input_schema": tool.inputSchema,
                            "server_config": server_config,
                            "aliases": aliases,
                        }

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
            if self.tool_registry
            else MCPInitState.FAILED
            if self.initialization_errors
            else MCPInitState.UNCONFIGURED
        )

    async def call_tool(self, tool_name: str, parameters: Dict[str, Any]) -> str:
        """Call a tool by name, return JSON string result."""
        if tool_name not in self.tool_registry:
            raise ToolNotFoundError(f"Tool '{tool_name}' not found. Available: {list(self.tool_registry.keys())}")

        server_name, original_tool_name = tool_name.split(":", 1)
        server_config: MCPServerConfig = self.tool_registry[tool_name]["server_config"]

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

    def register_tools(self, executor) -> None:
        """Register all discovered MCP tools onto a ToolExecutor.

        Each tool is wrapped in an MCPToolAdapter (a BaseTool) and registered
        under its namespaced name (server:tool_name) plus any user-configured
        aliases, so MCP tools share the executor's single dispatch path.

        Args:
            executor: ToolExecutor instance to register tools on.
        """

        for tool_name, tool_info in self.tool_registry.items():
            schema = {
                "name": tool_name,
                "description": tool_info.get("description", ""),
                "parameters": tool_info.get("input_schema", {}),
            }
            adapter = MCPToolAdapter(self, tool_name, schema)
            names = [tool_name] + list(tool_info.get("aliases", []))
            executor.register_tool_instance(adapter, names)

    def get_tool_schemas(self) -> dict[str, dict]:
        """Return schemas for all discovered MCP tools.

        Returns:
            dict mapping namespaced tool name -> schema dict with
            name, description, and parameters (input_schema).
        """
        schemas: dict[str, dict] = {}
        for tool_name, tool_info in self.tool_registry.items():
            schemas[tool_name] = {
                "name": tool_name,
                "description": tool_info.get("description", ""),
                "parameters": tool_info.get("input_schema", {}),
            }
        return schemas

    def get_status_description(self) -> str:
        """Human-readable status for system prompt."""
        if self.state == MCPInitState.READY:
            return f"MCP tools available ({len(self.tool_registry)}): {list(self.tool_registry.keys())}"
        elif self.state == MCPInitState.FAILED:
            return f"MCP tool discovery failed: {self.initialization_errors}"
        return "No external MCP tools configured."

    async def cleanup_clients(self) -> None:
        for server_name, client in list(self.clients.items()):
            try:
                await client.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing MCP client for {server_name}: {e}")
        self.clients.clear()

    async def _close_client(self, server_name: str) -> None:
        client = self.clients.pop(server_name, None)
        if client:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass

    def _build_client(self, server_config: MCPServerConfig) -> Client:
        if server_config.type == MCPTransportType.SSE:
            return Client(server_config.url)
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
