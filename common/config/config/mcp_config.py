from enum import Enum
from typing import Optional

from mote.common.utils.yaml_model import YamlModel
from pydantic import Field, model_validator


class MCPTransportType(str, Enum):
    SSE = "sse"
    STDIO = "stdio"


class MCPServerConfig(YamlModel):
    """Single MCP server configuration"""

    name: str = Field(default="default_name", description="Server name")
    type: MCPTransportType = Field(default=MCPTransportType.SSE, description="MCP server type")
    enabled: bool = Field(default=False, description="Whether to enable this server")

    # SSE type configuration
    url: Optional[str] = Field(default=None, description="URL for SSE type server")
    sse_read_timeout: Optional[float] = Field(
        default=60 * 10,
        description="Session read timeout in seconds, if the heartbeat is not received within this timeout, the session will be closed",
    )

    # STDIO type configuration
    command: Optional[str] = Field(default=None, description="Command for STDIO type server")
    args: Optional[list[str]] = Field(default_factory=list, description="Command arguments")

    # Common configuration
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    aliases: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Tool name aliases: {original_tool_name: [alias1, alias2]}",
    )
    tool_call_timeout: Optional[float] = Field(
        default=60 * 60,
        description="Tool call timeout in seconds, if the tool call is not completed within this timeout, the tool call will be cancelled",
    )

    @model_validator(mode="after")
    def validate_server_config(self):
        if not self.enabled:
            return self

        if self.type == MCPTransportType.SSE and not self.url:
            raise ValueError("URL must be provided for SSE type server")

        if self.type == MCPTransportType.STDIO and not self.command:
            raise ValueError("Command must be provided for STDIO type server")

        return self


class MCPConfig(YamlModel):
    """MCP configuration"""

    default_server: MCPServerConfig = Field(default_factory=MCPServerConfig, description="Internal server")
    servers: list[MCPServerConfig] = Field(default_factory=list, description="List of MCP servers")

    @model_validator(mode="after")
    def validate_unique_server_names(self):
        """Validate that all server names are unique"""
        if not self.servers:
            return self

        seen_names = set()
        for server in self.servers:
            if server.name in seen_names:
                raise ValueError(f"Duplicate mcp server name found: {server.name}")
            seen_names.add(server.name)

        return self

    def has_enabled_servers(self) -> bool:
        """Check if there is at least one enabled server"""
        return any(server.enabled for server in self.servers)
