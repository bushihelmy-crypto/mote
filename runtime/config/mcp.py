from typing import Optional

from pydantic import Field, model_validator

from mote.contracts.config.base import ConfigModel
from mote.contracts.config.model.oauth import OAuthProviderConfig
from mote.contracts.tool.transport import MCPTransportType


class MCPServerConfig(ConfigModel):
    """Configuration for one Runtime-managed MCP server."""

    name: str = Field(default="default_name", description="Server name")
    type: MCPTransportType = Field(default=MCPTransportType.SSE, description="MCP server type")
    enabled: bool = Field(default=False, description="Whether to enable this server")
    url: Optional[str] = Field(default=None, description="URL for SSE type server")
    sse_read_timeout: Optional[float] = Field(default=60 * 10)
    command: Optional[str] = Field(default=None, description="Command for STDIO type server")
    args: Optional[list[str]] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    tool_call_timeout: Optional[float] = Field(default=60 * 60)
    oauth: Optional[OAuthProviderConfig] = None

    @model_validator(mode="after")
    def validate_server_config(self):
        if not self.enabled:
            return self
        if self.type == MCPTransportType.SSE and not self.url:
            raise ValueError("URL must be provided for SSE type server")
        if self.type == MCPTransportType.STDIO and not self.command:
            raise ValueError("Command must be provided for STDIO type server")
        return self


__all__ = ["MCPServerConfig"]
