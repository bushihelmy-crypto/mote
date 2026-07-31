"""Stable tool-call and tool-effect contracts."""

from mote.contracts.tool.calls import serialize_tool_call_args
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.execution import ToolExecutionKind
from mote.contracts.tool.identity import ToolsetIdentity, ToolsetManifest, parse_toolset_manifest
from mote.contracts.tool.protocol import CommandProtocol, NativeToolSchema, ToolsetProtocolError, XmlToolSchema

__all__ = [
    "CommandProtocol",
    "NativeToolSchema",
    "ToolEffect",
    "ToolExecutionKind",
    "ToolsetProtocolError",
    "ToolsetIdentity",
    "ToolsetManifest",
    "parse_toolset_manifest",
    "XmlToolSchema",
    "serialize_tool_call_args",
]
from mote.contracts.tool.catalog import (
    MaterializedToolCatalog,
    MaterializedToolDefinition,
    ToolBindingSnapshot,
    ToolCatalogIdentity,
    ToolDispatchRequest,
    ToolDispatchResult,
    ToolExecutionOutcome,
    ToolExecutionPort,
)

__all__ += [
    "MaterializedToolCatalog",
    "MaterializedToolDefinition",
    "ToolBindingSnapshot",
    "ToolCatalogIdentity",
    "ToolDispatchRequest",
    "ToolDispatchResult",
    "ToolExecutionOutcome",
    "ToolExecutionPort",
]
