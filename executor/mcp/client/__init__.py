from typing import Optional, Union

from mote.common.config.config.mcp_config import MCPServerConfig, MCPTransportType
from mote.common.config.loader import load_config
from mote.executor.mcp.client.sse import MCPSSEClient
from mote.executor.mcp.client.stdio import MCPStdioClient


def get_mcp_client(server_config: Optional[MCPServerConfig] = None) -> Union[MCPSSEClient, MCPStdioClient]:
    """Get the appropriate MCP client based on the server configuration.

    Args:
        server_config: The server configuration to use. If None, the default server configuration will be used.

    Returns:
        The appropriate MCP client based on the server configuration.
    """
    server_config = server_config or load_config().mcp.default_server

    if server_config.type == MCPTransportType.SSE:
        return MCPSSEClient(server_config)

    if server_config.type == MCPTransportType.STDIO:
        return MCPStdioClient(server_config)

    raise TypeError(f"Unknown mcp server config: {server_config}")
