"""Runtime-bound XML and Native tool provider definitions.

Definitions own the model-facing contract.  Their capability factory is an
opaque Kernel handle interpreted by Runtime during binding; Kernel never
imports the concrete execution base class.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from mote.contracts.events.envelope import JsonValue
from mote.contracts.tool.execution import ToolExecutionKind
from mote.contracts.tool.protocol import CommandProtocol, NativeToolSchema, XmlToolSchema
from mote.runtime.tools.base_tool import BaseTool

XmlSchemaRenderer = Callable[[BaseTool], XmlToolSchema]
NativeSchemaRenderer = Callable[[BaseTool], NativeToolSchema]
ArgumentDecoder = Callable[[dict[str, JsonValue]], dict[str, JsonValue]]


def _identity_arguments(arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return arguments


def _prefixed_names(namespace: str, name: str, aliases: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    marker = f"{namespace}_"
    return marker + name, tuple(marker + alias for alias in aliases)


@dataclass(frozen=True, slots=True)
class XmlToolDefinition:
    """One explicit registration on the XML prompt/catalog boundary."""

    name: str
    capability_factory: Callable[[], BaseTool]
    capability_type: type[BaseTool]
    schema_renderer: XmlSchemaRenderer
    source_identity: str
    argument_decoder: ArgumentDecoder = _identity_arguments
    aliases: tuple[str, ...] = ()
    description: str = ""
    summary: str = ""
    search_text: str = ""
    category: str = "builtin"
    execution_kind: ToolExecutionKind = ToolExecutionKind.ATOMIC
    approval_required: bool = False

    @property
    def protocol(self) -> CommandProtocol:
        return CommandProtocol.XML

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def render(self, capability: BaseTool) -> XmlToolSchema:
        schema = self.schema_renderer(capability)
        return {
            "name": self.name,
            "description": schema["description"],
            "parameters": dict(schema["parameters"]),
        }

    def prefixed(self, namespace: str) -> "XmlToolDefinition":
        name, aliases = _prefixed_names(namespace, self.name, self.aliases)
        return replace(self, name=name, aliases=aliases)

    def renamed(self, name: str) -> "XmlToolDefinition":
        return replace(self, name=name)

    def requiring_approval(self) -> "XmlToolDefinition":
        return replace(self, approval_required=True)


@dataclass(frozen=True, slots=True)
class NativeToolDefinition:
    """One explicit registration on the provider-native tool boundary."""

    name: str
    capability_factory: Callable[[], BaseTool]
    capability_type: type[BaseTool]
    schema_renderer: NativeSchemaRenderer
    source_identity: str
    argument_decoder: ArgumentDecoder = _identity_arguments
    aliases: tuple[str, ...] = ()
    description: str = ""
    summary: str = ""
    search_text: str = ""
    category: str = "builtin"
    execution_kind: ToolExecutionKind = ToolExecutionKind.ATOMIC
    approval_required: bool = False

    @property
    def protocol(self) -> CommandProtocol:
        return CommandProtocol.NATIVE

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def render(self, capability: BaseTool) -> NativeToolSchema:
        schema = self.schema_renderer(capability)
        return {
            "name": self.name,
            "description": schema["description"],
            "input_schema": dict(schema["input_schema"]),
        }

    def prefixed(self, namespace: str) -> "NativeToolDefinition":
        name, aliases = _prefixed_names(namespace, self.name, self.aliases)
        return replace(self, name=name, aliases=aliases)

    def renamed(self, name: str) -> "NativeToolDefinition":
        return replace(self, name=name)

    def requiring_approval(self) -> "NativeToolDefinition":
        return replace(self, approval_required=True)


ToolDefinition = XmlToolDefinition | NativeToolDefinition

__all__ = [
    "NativeSchemaRenderer",
    "NativeToolDefinition",
    "ToolDefinition",
    "XmlSchemaRenderer",
    "ArgumentDecoder",
    "XmlToolDefinition",
]
