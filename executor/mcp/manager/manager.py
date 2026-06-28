"""MCP client manager responsible for managing the lifecycle of all MCP clients"""

import asyncio
import re
import threading
from functools import partial
from typing import Callable, Optional

from metagpt.common.config.config.mcp_config import MCPServerConfig
from metagpt.executor.mcp.client import get_mcp_client
from metagpt.executor.mcp.client.base import MCPBaseClient
from metagpt.executor.mcp.mcp_registry import MCP_REGISTRY
from metagpt.common.logs import logger
from metagpt.common.utils.async_helper import run_coroutine_sync
from metagpt.common.config.loader import load_config

class MCPClientManager:
    """MCP client manager responsible for managing the lifecycle of all MCP clients and tool registration"""

    def __init__(self):
        self.tool_executors: dict[str, Callable] = {}  # Store tool executors
        self.mcp_clients: dict[str, MCPBaseClient] = {}  # Cache MCP clients
        self._tools_registered = False
        self._registration_lock = threading.Lock()

    def ensure_tools_registered(self):
        """
        Ensure tools are registered, start registration process if not already done.
        This method is thread-safe.
        """
        if self._tools_registered:
            return

        with self._registration_lock:
            # Double-checked
            if self._tools_registered:
                return

            run_coroutine_sync(self.register_tools())
            self._tools_registered = True

    async def register_tools(self, server_configs: Optional[list[MCPServerConfig]] = None):
        """Register all tools from MCP servers"""

        if self._tools_registered:
            return

        if server_configs is None:
            config = load_config()
            server_configs = config.mcp.servers

        for server_config in server_configs:
            if not server_config.enabled:
                continue

            try:
                mcp_client = get_mcp_client(server_config)

                # Connect and get tool list
                tools = await mcp_client.list_tools()

                # Cleanup client to avoid "RuntimeError: Attempted to exit cancel scope in a different task than it was entered in" when use `run_coroutine_sync`
                await mcp_client.cleanup()

                # Create namespace to prevent tool name conflicts across servers
                namespace = self._create_mcp_namespace(server_config.name)

                # Register tools
                for tool in tools:
                    original_tool_name = tool.name
                    namespaced_tool_name = f"{namespace}_{original_tool_name}"

                    # Register tool with namespaced_tool_name
                    tool.name = namespaced_tool_name
                    MCP_REGISTRY.register_mcp_tool(tool)

                    # Store executor, and call mcp server with original_tool_name
                    self.tool_executors[namespaced_tool_name] = partial(
                        self._mcp_tool_executor, server_config, original_tool_name
                    )

            except Exception as e:
                logger.warning(f"Failed to register tools from {server_config.name}: {e}")

        self._tools_registered = True

    def get_tool_executor(self, tool_name: str) -> Optional[Callable]:
        """Get the executor function for a specific tool by its name"""
        # Defer Config instantiation
        self.ensure_tools_registered()
        return self.tool_executors.get(tool_name)

    def list_tool_names(self) -> list[str]:
        """List all tool names"""
        # Defer Config instantiation
        self.ensure_tools_registered()
        return list(self.tool_executors.keys())

    async def close_all_clients(self):
        """Close every MCP client in parallel."""
        if not self.mcp_clients:
            return

        close_tasks = [self._close_client(key) for key in self.mcp_clients.keys()]
        await asyncio.gather(*close_tasks, return_exceptions=True)

    def _create_mcp_namespace(self, server_name: str) -> str:
        """
        Create namespace prefix for MCP tools to avoid naming conflicts

        Args:
            server_name: MCP server name from config

        Returns:
            Sanitized namespace prefix for tools

        Examples:
            "GitHub API" -> "github_api"
            "playwright-browser" -> "playwright_browser"
            "Jira Tools 2.0" -> "jira_tools_2_0"
            "123server" -> "mcp_123server"
            "" -> "unnamed_mcp"
        """
        if not server_name.strip():
            return "unnamed_mcp"

        namespace = re.sub(r"[^a-z0-9]+", "_", server_name.lower()).strip("_")
        return f"mcp_{namespace}" if namespace and namespace[0].isdigit() else namespace or "unnamed_mcp"

    async def _mcp_tool_executor(self, server_config: MCPServerConfig, tool_name: str, **kwargs):
        """Execute tool with cached client"""
        server_key = server_config.name

        # Get or create cached client
        if server_key not in self.mcp_clients:
            self.mcp_clients[server_key] = get_mcp_client(server_config)

        return await self.mcp_clients[server_key].call_tool(tool_name, arguments=kwargs)

    async def _close_client(self, server_key: str):
        """Close a specific client"""
        try:
            await self.mcp_clients[server_key].cleanup()
        finally:
            self.mcp_clients.pop(server_key, None)


mcp_manager = MCPClientManager()
