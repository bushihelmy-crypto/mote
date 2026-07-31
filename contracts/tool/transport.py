from enum import Enum


class MCPTransportType(str, Enum):
    SSE = "sse"
    STDIO = "stdio"


__all__ = ["MCPTransportType"]
