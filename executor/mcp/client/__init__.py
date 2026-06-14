from metagpt.common.config.mcp_config import MCPServerConfig, MCPTransportType
from metagpt.executor.mcp.client.sse import MCPSSEClient
from metagpt.executor.mcp.client.stdio import MCPStdioClient
from metagpt.common.config2 import Config

from typing import Union


def get_mcp_client(server_config: MCPServerConfig = None) -> Union[MCPSSEClient, MCPStdioClient]:
    """Get the appropriate MCP client based on the server configuration.

    Args:
        server_config: The server configuration to use. If None, the default server configuration will be used.

    Returns:
        The appropriate MCP client based on the server configuration.
    """
    server_config = server_config or Config.default().mcp.default_server

    if server_config.type == MCPTransportType.SSE:
        return MCPSSEClient(server_config)

    if server_config.type == MCPTransportType.STDIO:
        return MCPStdioClient(server_config)

    raise TypeError(f"Unknown mcp server config: {server_config}")
