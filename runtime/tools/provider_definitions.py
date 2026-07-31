"""Runtime-bound XML and Native tool provider definitions.

Definitions own the model-facing contract.  Their capability factory is an
opaque Kernel handle interpreted by Runtime during binding; Kernel never
imports the concrete execution base class.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Generic, TypeVar

from mote.contracts.tool.execution import ToolExecutionKind
from mote.contracts.tool.protocol import CommandProtocol, NativeToolSchema, XmlToolSchema

CapabilityT = TypeVar("CapabilityT")
XmlSchemaRenderer = Callable[[CapabilityT], XmlToolSchema]
NativeSchemaRenderer = Callable[[CapabilityT], NativeToolSchema]
ArgumentDecoder = Callable[[dict[str, Any]], dict[str, Any]]


def _identity_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return arguments


def _prefixed_names(namespace: str, name: str, aliases: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    marker = f"{namespace}_"
    return marker + name, tuple(marker + alias for alias in aliases)


@dataclass(frozen=True, slots=True)
class XmlToolDefinition(Generic[CapabilityT]):
    """One explicit registration on the XML prompt/catalog boundary."""

    name: str
    capability_factory: Callable[[], CapabilityT]
    capability_type: type
    schema_renderer: XmlSchemaRenderer[CapabilityT]
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

    def render(self, capability: CapabilityT) -> XmlToolSchema:
        schema = dict(self.schema_renderer(capability))
        schema["name"] = self.name
        return schema  # type: ignore[return-value]

    def prefixed(self, namespace: str) -> "XmlToolDefinition[CapabilityT]":
        name, aliases = _prefixed_names(namespace, self.name, self.aliases)
        return replace(self, name=name, aliases=aliases)

    def renamed(self, name: str) -> "XmlToolDefinition[CapabilityT]":
        return replace(self, name=name)

    def requiring_approval(self) -> "XmlToolDefinition[CapabilityT]":
        return replace(self, approval_required=True)


@dataclass(frozen=True, slots=True)
class NativeToolDefinition(Generic[CapabilityT]):
    """One explicit registration on the provider-native tool boundary."""

    name: str
    capability_factory: Callable[[], CapabilityT]
    capability_type: type
    schema_renderer: NativeSchemaRenderer[CapabilityT]
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

    def render(self, capability: CapabilityT) -> NativeToolSchema:
        schema = dict(self.schema_renderer(capability))
        schema["name"] = self.name
        return schema  # type: ignore[return-value]

    def prefixed(self, namespace: str) -> "NativeToolDefinition[CapabilityT]":
        name, aliases = _prefixed_names(namespace, self.name, self.aliases)
        return replace(self, name=name, aliases=aliases)

    def renamed(self, name: str) -> "NativeToolDefinition[CapabilityT]":
        return replace(self, name=name)

    def requiring_approval(self) -> "NativeToolDefinition[CapabilityT]":
        return replace(self, approval_required=True)


ToolDefinition = XmlToolDefinition[Any] | NativeToolDefinition[Any]

__all__ = [
    "NativeSchemaRenderer",
    "NativeToolDefinition",
    "ToolDefinition",
    "XmlSchemaRenderer",
    "ArgumentDecoder",
    "XmlToolDefinition",
]
