"""Nominal XML and Native tool definitions.

Definitions own the model-facing contract.  Their capability factory is an
opaque Kernel handle interpreted by Runtime during binding; Kernel never
imports the concrete execution base class.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Generic, TypeVar

from mote.contracts.run_context import RunContext
from mote.contracts.tools.protocol import CommandProtocol, NativeToolSchema, XmlToolSchema

CapabilityT = TypeVar("CapabilityT")
XmlSchemaRenderer = Callable[[CapabilityT], XmlToolSchema]
NativeSchemaRenderer = Callable[[CapabilityT], NativeToolSchema]
ArgumentDecoder = Callable[[dict[str, Any]], dict[str, Any]]
ApprovalPredicate = Callable[[RunContext[Any], Mapping[str, Any]], bool]


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
    approval_required: bool = False
    approval_predicate: ApprovalPredicate | None = None

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

    def requiring_approval(
        self,
        predicate: ApprovalPredicate | None = None,
    ) -> "XmlToolDefinition[CapabilityT]":
        if predicate is None:
            return replace(self, approval_required=True)
        if self.approval_required:
            return self
        existing = self.approval_predicate
        if existing is None:
            return replace(self, approval_predicate=predicate)
        return replace(
            self,
            approval_predicate=lambda ctx, args: existing(ctx, args) or predicate(ctx, args),
        )


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
    approval_required: bool = False
    approval_predicate: ApprovalPredicate | None = None

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

    def requiring_approval(
        self,
        predicate: ApprovalPredicate | None = None,
    ) -> "NativeToolDefinition[CapabilityT]":
        if predicate is None:
            return replace(self, approval_required=True)
        if self.approval_required:
            return self
        existing = self.approval_predicate
        if existing is None:
            return replace(self, approval_predicate=predicate)
        return replace(
            self,
            approval_predicate=lambda ctx, args: existing(ctx, args) or predicate(ctx, args),
        )


ToolDefinition = XmlToolDefinition[Any] | NativeToolDefinition[Any]

__all__ = [
    "NativeSchemaRenderer",
    "NativeToolDefinition",
    "ApprovalPredicate",
    "ToolDefinition",
    "XmlSchemaRenderer",
    "ArgumentDecoder",
    "XmlToolDefinition",
]
