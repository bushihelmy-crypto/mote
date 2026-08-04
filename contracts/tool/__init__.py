"""Stable tool-call and tool-effect contracts."""

from mote.contracts.tool.arguments import ToolArguments, freeze_tool_arguments
from mote.contracts.tool.calls import serialize_tool_call_args
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.execution import ToolExecutionKind
from mote.contracts.tool.identity import (
    ToolAttemptOrdinal,
    ToolInvocationId,
    ToolInvocationIdentity,
    ToolsetIdentity,
    ToolsetManifest,
    parse_toolset_manifest,
    tool_arguments_digest,
)
from mote.contracts.tool.protocol import CommandProtocol, NativeToolSchema, ToolsetProtocolError, XmlToolSchema

__all__ = [
    "CommandProtocol",
    "ToolArguments",
    "NativeToolSchema",
    "ToolEffect",
    "ToolExecutionKind",
    "ToolAttemptOrdinal",
    "ToolInvocationId",
    "ToolInvocationIdentity",
    "ToolsetProtocolError",
    "ToolsetIdentity",
    "ToolsetManifest",
    "parse_toolset_manifest",
    "XmlToolSchema",
    "serialize_tool_call_args",
    "tool_arguments_digest",
    "freeze_tool_arguments",
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
