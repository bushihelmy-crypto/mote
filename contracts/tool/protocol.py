"""Command-protocol identities and model-facing tool schema contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any, TypedDict


class CommandProtocol(str, Enum):
    """The wire protocol used to expose and receive model tool calls."""

    XML = "xml"
    NATIVE = "native"


class XmlToolSchema(TypedDict):
    """Prompt-catalog schema understood by the XML command parser."""

    name: str
    description: str
    parameters: dict[str, Any]


class NativeToolSchema(TypedDict):
    """Provider-independent structured tool definition for native tool use."""

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolsetProtocolError(TypeError):
    """A Toolset crossed an incompatible command-protocol boundary."""


__all__ = [
    "CommandProtocol",
    "NativeToolSchema",
    "ToolsetProtocolError",
    "XmlToolSchema",
]
