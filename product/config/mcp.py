from pydantic import Field

from mote.product.config.base import ConfigModel


class MCPConfig(ConfigModel):
    """Product-level master switch for MCP integration."""

    enabled: bool = Field(default=False, description="MCP master switch.")


__all__ = ["MCPConfig"]
