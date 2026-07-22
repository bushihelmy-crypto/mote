from enum import Enum
from typing import Optional

from pydantic import Field, model_validator

from mote.common.config.config.oauth_config import OAuthProviderConfig
from mote.common.utils.yaml_model import YamlModel


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

    # Optional OAuth for a remote (SSE/HTTP) server that requires a bearer token.
    # When set, the MCP client authenticates via the shared OAuth runtime
    # (``router.oauth.OAuthManager``) instead of a static header; STDIO servers
    # are local processes and ignore this.
    oauth: Optional[OAuthProviderConfig] = Field(
        default=None, description="OAuth settings for a remote server that requires a bearer token (SSE only)."
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
    """MCP subsystem master switch (mirrors the Skills master switch).

    The *servers* themselves are never configured here — they live in their own
    standard ``mcp_config.json`` (discovered by
    ``executor.mcp.config_source.load_mcp_servers``). This section carries only
    the global on/off toggle: when ``enabled`` the subsystem engages and every
    server present in ``mcp_config.json`` is loaded, exactly as
    ``context.skills.enabled`` engages the Skills index. A role may still opt in
    to specific servers via ``role_schema.mcps`` even with the switch off.
    """

    enabled: bool = Field(default=False, description="MCP master switch.")
