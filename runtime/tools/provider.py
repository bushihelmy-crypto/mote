"""Runtime ToolProvider lifecycle and protocol adapter algebra."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from abc import ABC
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any, Generic, TypeAlias, TypeVar

from mote.contracts.tool.identity import ToolsetIdentity, ToolsetManifest
from mote.contracts.tool.protocol import CommandProtocol, ToolsetProtocolError
from mote.kernel.execution.run_context import RunContext
from mote.runtime.tools.provider_definitions import NativeToolDefinition, ToolDefinition, XmlToolDefinition
from mote.runtime.tools.tool_binding import BoundApprovalPolicy

DefinitionT = TypeVar("DefinitionT", XmlToolDefinition[Any], NativeToolDefinition[Any])
AgentDepsT = TypeVar("AgentDepsT", contravariant=True)
ToolFilter = Callable[[DefinitionT], bool]
ToolDefinitionsPrepare: TypeAlias = Callable[[tuple[DefinitionT, ...]], Iterable[DefinitionT]]
XmlApprovalPolicy: TypeAlias = Callable[
    [RunContext[AgentDepsT], XmlToolDefinition[Any], Mapping[str, Any]],
    bool,
]
NativeApprovalPolicy: TypeAlias = Callable[
    [RunContext[AgentDepsT], NativeToolDefinition[Any], Mapping[str, Any]],
    bool,
]


ToolsetPolicy: TypeAlias = Callable[[RunContext[AgentDepsT], ToolDefinition, Mapping[str, Any]], bool]


class ToolsetConflictError(ValueError):
    """Multiple Toolsets claim the same model-facing dispatch name."""


class ToolsetCompositionError(ValueError):
    """A Toolset transformation has an invalid value."""


class DefinitionSource(ABC, Generic[DefinitionT]):
    """Read-only definition source shared by the two nominal protocol types.

    This base deliberately has no public composition methods.  XML and Native
    subclasses provide protocol-specific signatures so a static checker cannot
    accept cross-protocol composition.
    """

    def __init__(
        self,
        definitions: Callable[[], Iterable[DefinitionT]] | Iterable[DefinitionT],
        *,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        if callable(definitions):
            self._definitions_source = definitions
        else:
            snapshot = tuple(definitions)
            self._definitions_source = lambda: snapshot
        self._prepare_callback = prepare

    def prepare(self) -> None:
        if self._prepare_callback is not None:
            self._prepare_callback()

    def _snapshot(self) -> tuple[DefinitionT, ...]:
        self.prepare()
        return tuple(self._definitions_source())

    @staticmethod
    def _validate_unique_names(definitions: Sequence[DefinitionT]) -> None:
        owners: dict[str, str] = {}
        for definition in definitions:
            for name in definition.names:
                previous = owners.get(name)
                if previous is not None:
                    raise ToolsetConflictError(
                        f"tool {name!r} is declared by both {previous!r} and {definition.name!r}"
                    )
                owners[name] = definition.name


class Toolset(DefinitionSource[DefinitionT], Generic[DefinitionT, AgentDepsT]):
    protocol: CommandProtocol

    def __init__(
        self,
        id: str,
        definitions: Callable[[], Iterable[DefinitionT]] | Iterable[DefinitionT],
        *,
        version: str = "1",
        prepare: Callable[[], None] | None = None,
        requires_permission_gate: bool = False,
        approval_policy: ToolsetPolicy[AgentDepsT] | None = None,
        approval_mutating_only: bool = False,
        instructions: str | Iterable[str] = (),
    ) -> None:
        super().__init__(definitions, prepare=prepare)
        normalized = id.strip()
        if not normalized:
            raise ToolsetCompositionError("Toolset id must not be empty")
        self._id = normalized
        self._identity = ToolsetIdentity(
            id=normalized,
            version=version,
            protocol=self.protocol,
        )
        self._requires_permission_gate = requires_permission_gate
        self._approval_policy = approval_policy
        self._approval_mutating_only = approval_mutating_only
        self._bound_context = None
        self._static_instruction_blocks = _normalized_instruction_blocks(instructions)

    @property
    def id(self) -> str:
        return self._id

    @property
    def version(self) -> str:
        """Application-managed semantic version used for durable recovery."""

        return self._identity.version

    @property
    def identity(self) -> ToolsetIdentity:
        """Protocol-explicit identity recorded in the session manifest."""

        return self._identity

    @property
    def requires_permission_gate(self) -> bool:
        return self._requires_permission_gate

    @property
    def static_instruction_blocks(self) -> tuple[str, ...]:
        """Session-stable instructions safe to place in the system prompt."""

        return self._static_instruction_blocks

    @property
    def dynamic_instruction_blocks(self) -> tuple[str, ...]:
        """Active run/step instructions that must use request-only context."""

        return ()

    @property
    def instruction_blocks(self) -> tuple[str, ...]:
        """All instructions exposed by the current Toolset view."""

        return _dedupe_instruction_blocks((*self.static_instruction_blocks, *self.dynamic_instruction_blocks))

    @property
    def changes_per_run_step(self) -> bool:
        """Whether Runtime must refresh this Toolset before every model step."""

        return False

    @property
    def run_scoped_lifecycle(self) -> bool:
        """Whether definitions become invalid after the run context exits."""

        return False

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> "Toolset[DefinitionT, AgentDepsT]":
        """Return the Toolset instance owned by one Agent run."""

        self._bind_run_context(ctx)
        return self

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> "Toolset[DefinitionT, AgentDepsT]":
        """Refresh per-step state and return the active Toolset instance."""

        return self

    async def __aenter__(self) -> "Toolset[DefinitionT, AgentDepsT]":
        return self

    async def __aexit__(self, *_exc: object) -> bool | None:
        return None

    def definitions(self) -> tuple[DefinitionT, ...]:
        definitions = self._snapshot()
        for definition in definitions:
            if definition.protocol is not self.protocol:
                raise ToolsetProtocolError(
                    f"Toolset {self.id!r} is {self.protocol.value} but contains "
                    f"{definition.protocol.value} definition {definition.name!r}"
                )
        self._validate_unique_names(definitions)
        return definitions

    def get(self, name: str) -> DefinitionT | None:
        matches = [definition for definition in self.definitions() if name in definition.names]
        if not matches:
            return None
        if len(matches) > 1:
            raise ToolsetConflictError(f"tool {name!r} is declared more than once in Toolset {self.id!r}")
        return matches[0]

    def names_for(self, definition: DefinitionT) -> list[str]:
        return list(definition.names)

    def tool_names(self) -> tuple[str, ...]:
        return tuple(definition.name for definition in self.definitions())

    def requires_approval(self, definition: DefinitionT) -> bool:
        return definition.approval_required or self._approval_policy is not None

    def bind_approval(self, definition: ToolDefinition) -> BoundApprovalPolicy | None:
        if self._approval_policy is None:
            return None
        if self._approval_mutating_only and not bool(getattr(definition.capability_type, "mutates_filesystem", False)):
            return None
        context = self._bound_context
        if context is None:
            return BoundApprovalPolicy(lambda _arguments: True)
        policy = self._approval_policy
        return BoundApprovalPolicy(lambda arguments: policy(context, definition, arguments))

    def approval_applies(self, definition: ToolDefinition) -> bool:
        return self._approval_policy is not None and (
            not self._approval_mutating_only or bool(getattr(definition.capability_type, "mutates_filesystem", False))
        )

    def _bind_run_context(self, context: RunContext[AgentDepsT]) -> None:
        self._bound_context = context


class XmlToolset(Toolset[XmlToolDefinition[Any], AgentDepsT], Generic[AgentDepsT]):
    """A composable source containing XML definitions only."""

    protocol = CommandProtocol.XML

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> "XmlToolset[AgentDepsT]":
        self._bind_run_context(ctx)
        return self

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> "XmlToolset[AgentDepsT]":
        return self

    def filter(self, policy: ToolFilter[XmlToolDefinition[Any]]) -> "XmlToolset[AgentDepsT]":
        return _XmlToolsetView(
            f"filter:{self.id}",
            self,
            lambda definitions: (definition for definition in definitions if policy(definition)),
        )

    def prefix(self, prefix: str) -> "XmlToolset[AgentDepsT]":
        normalized = _validated_prefix(prefix)
        return _XmlToolsetView(
            f"{normalized}:{self.id}",
            self,
            lambda definitions: (definition.prefixed(normalized) for definition in definitions),
        )

    def rename(self, mapping: Mapping[str, str]) -> "XmlToolset[AgentDepsT]":
        """Rename canonical XML dispatch names using ``old_name -> new_name``."""

        normalized = _validated_rename_mapping(mapping)
        return _XmlToolsetView(
            f"rename:{self.id}",
            self,
            lambda definitions: _renamed_xml_definitions(self.id, definitions, normalized),
        )

    def prepared(
        self,
        prepare: ToolDefinitionsPrepare[XmlToolDefinition[Any]],
    ) -> "XmlToolset[AgentDepsT]":
        """Return an immutable XML definition-preparation view."""

        return _XmlToolsetView(
            f"prepared:{self.id}",
            self,
            lambda definitions: _prepared_definitions(self.id, self.protocol, definitions, prepare),
        )

    def with_approval(
        self,
        policy: XmlApprovalPolicy[AgentDepsT] | None = None,
        *,
        mutating_only: bool = False,
    ) -> "XmlToolset[AgentDepsT]":
        _validate_approval_policy(policy)
        if policy is None:
            policy = lambda _context, _definition, _arguments: True
        view = _XmlToolsetView(
            f"approval:{self.id}",
            self,
            lambda definitions: definitions,
            requires_permission_gate=True,
            approval_policy=(
                (
                    lambda context, definition, arguments: policy(
                        context,
                        _require_xml_definition(definition),
                        arguments,
                    )
                )
                if policy is not None
                else None
            ),
            approval_mutating_only=mutating_only,
        )
        return view

    def with_instructions(self, *instructions: str) -> "XmlToolset[AgentDepsT]":
        return _XmlToolsetView(
            f"instructions:{self.id}",
            self,
            lambda definitions: definitions,
            instructions=instructions,
        )

    def combine(self, *others: "XmlToolset[AgentDepsT]") -> "XmlToolset[AgentDepsT]":
        toolsets = (self, *others)
        _validate_protocol_toolsets(CommandProtocol.XML, toolsets)
        return _CombinedXmlToolset(
            "+".join(toolset.id for toolset in toolsets),
            toolsets,
        )


class NativeToolset(Toolset[NativeToolDefinition[Any], AgentDepsT], Generic[AgentDepsT]):
    """A composable source containing Native definitions only."""

    protocol = CommandProtocol.NATIVE

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> "NativeToolset[AgentDepsT]":
        self._bind_run_context(ctx)
        return self

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> "NativeToolset[AgentDepsT]":
        return self

    def filter(self, policy: ToolFilter[NativeToolDefinition[Any]]) -> "NativeToolset[AgentDepsT]":
        return _NativeToolsetView(
            f"filter:{self.id}",
            self,
            lambda definitions: (definition for definition in definitions if policy(definition)),
        )

    def prefix(self, prefix: str) -> "NativeToolset[AgentDepsT]":
        normalized = _validated_prefix(prefix)
        return _NativeToolsetView(
            f"{normalized}:{self.id}",
            self,
            lambda definitions: (definition.prefixed(normalized) for definition in definitions),
        )

    def rename(self, mapping: Mapping[str, str]) -> "NativeToolset[AgentDepsT]":
        """Rename canonical Native dispatch names using ``old_name -> new_name``."""

        normalized = _validated_rename_mapping(mapping)
        return _NativeToolsetView(
            f"rename:{self.id}",
            self,
            lambda definitions: _renamed_native_definitions(self.id, definitions, normalized),
        )

    def prepared(
        self,
        prepare: ToolDefinitionsPrepare[NativeToolDefinition[Any]],
    ) -> "NativeToolset[AgentDepsT]":
        """Return an immutable Native definition-preparation view."""

        return _NativeToolsetView(
            f"prepared:{self.id}",
            self,
            lambda definitions: _prepared_definitions(self.id, self.protocol, definitions, prepare),
        )

    def with_approval(
        self,
        policy: NativeApprovalPolicy[AgentDepsT] | None = None,
        *,
        mutating_only: bool = False,
    ) -> "NativeToolset[AgentDepsT]":
        _validate_approval_policy(policy)
        if policy is None:
            policy = lambda _context, _definition, _arguments: True
        view = _NativeToolsetView(
            f"approval:{self.id}",
            self,
            lambda definitions: definitions,
            requires_permission_gate=True,
            approval_policy=(
                (
                    lambda context, definition, arguments: policy(
                        context,
                        _require_native_definition(definition),
                        arguments,
                    )
                )
                if policy is not None
                else None
            ),
            approval_mutating_only=mutating_only,
        )
        return view

    def with_instructions(self, *instructions: str) -> "NativeToolset[AgentDepsT]":
        return _NativeToolsetView(
            f"instructions:{self.id}",
            self,
            lambda definitions: definitions,
            instructions=instructions,
        )

    def combine(self, *others: "NativeToolset[AgentDepsT]") -> "NativeToolset[AgentDepsT]":
        toolsets = (self, *others)
        _validate_protocol_toolsets(CommandProtocol.NATIVE, toolsets)
        return _CombinedNativeToolset(
            "+".join(toolset.id for toolset in toolsets),
            toolsets,
        )


XmlTransform: TypeAlias = Callable[
    [tuple[XmlToolDefinition[Any], ...]],
    Iterable[XmlToolDefinition[Any]],
]
NativeTransform: TypeAlias = Callable[
    [tuple[NativeToolDefinition[Any], ...]],
    Iterable[NativeToolDefinition[Any]],
]


class _XmlToolsetView(XmlToolset[AgentDepsT]):
    def __init__(
        self,
        id: str,
        wrapped: XmlToolset[AgentDepsT],
        transform: XmlTransform,
        *,
        requires_permission_gate: bool = False,
        approval_policy: ToolsetPolicy[AgentDepsT] | None = None,
        approval_mutating_only: bool = False,
        instructions: str | Iterable[str] = (),
    ) -> None:
        self._wrapped = wrapped
        self._transform = transform
        self._force_permission_gate = requires_permission_gate
        self._view_approval_policy = approval_policy
        self._view_approval_mutating_only = approval_mutating_only
        self._own_instruction_blocks = _normalized_instruction_blocks(instructions)
        super().__init__(
            id,
            self._view_definitions,
            version=wrapped.version,
            requires_permission_gate=(requires_permission_gate or wrapped.requires_permission_gate),
            approval_policy=approval_policy,
            approval_mutating_only=approval_mutating_only,
            instructions=(
                *wrapped.static_instruction_blocks,
                *self._own_instruction_blocks,
            ),
        )

    def bind_approval(self, definition: ToolDefinition) -> BoundApprovalPolicy | None:
        return super().bind_approval(definition) or self._wrapped.bind_approval(definition)

    def _bind_run_context(self, context: RunContext[AgentDepsT]) -> None:
        super()._bind_run_context(context)
        self._wrapped._bind_run_context(context)

    def _view_definitions(self) -> Iterable[XmlToolDefinition[Any]]:
        return tuple(self._transform(self._wrapped.definitions()))

    @property
    def changes_per_run_step(self) -> bool:
        return self._wrapped.changes_per_run_step

    @property
    def run_scoped_lifecycle(self) -> bool:
        return self._wrapped.run_scoped_lifecycle

    @property
    def dynamic_instruction_blocks(self) -> tuple[str, ...]:
        return self._wrapped.dynamic_instruction_blocks

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> XmlToolset[AgentDepsT]:
        wrapped = await self._wrapped.for_run(ctx)
        if wrapped is self._wrapped:
            self._bind_run_context(ctx)
            return self
        return _XmlToolsetView(
            self.id,
            wrapped,
            self._transform,
            requires_permission_gate=self._force_permission_gate,
            approval_policy=self._view_approval_policy,
            approval_mutating_only=self._view_approval_mutating_only,
            instructions=self._own_instruction_blocks,
        )

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> XmlToolset[AgentDepsT]:
        wrapped = await self._wrapped.for_run_step(ctx)
        if wrapped is self._wrapped:
            return self
        return _XmlToolsetView(
            self.id,
            wrapped,
            self._transform,
            requires_permission_gate=self._force_permission_gate,
            approval_policy=self._view_approval_policy,
            approval_mutating_only=self._view_approval_mutating_only,
            instructions=self._own_instruction_blocks,
        )

    async def __aenter__(self) -> XmlToolset[AgentDepsT]:
        await self._wrapped.__aenter__()
        return self

    async def __aexit__(self, *_exc: object) -> bool | None:
        return await self._wrapped.__aexit__(*_exc)


class _NativeToolsetView(NativeToolset[AgentDepsT]):
    def __init__(
        self,
        id: str,
        wrapped: NativeToolset[AgentDepsT],
        transform: NativeTransform,
        *,
        requires_permission_gate: bool = False,
        approval_policy: ToolsetPolicy[AgentDepsT] | None = None,
        approval_mutating_only: bool = False,
        instructions: str | Iterable[str] = (),
    ) -> None:
        self._wrapped = wrapped
        self._transform = transform
        self._force_permission_gate = requires_permission_gate
        self._view_approval_policy = approval_policy
        self._view_approval_mutating_only = approval_mutating_only
        self._own_instruction_blocks = _normalized_instruction_blocks(instructions)
        super().__init__(
            id,
            self._view_definitions,
            version=wrapped.version,
            requires_permission_gate=(requires_permission_gate or wrapped.requires_permission_gate),
            approval_policy=approval_policy,
            approval_mutating_only=approval_mutating_only,
            instructions=(
                *wrapped.static_instruction_blocks,
                *self._own_instruction_blocks,
            ),
        )

    def bind_approval(self, definition: ToolDefinition) -> BoundApprovalPolicy | None:
        return super().bind_approval(definition) or self._wrapped.bind_approval(definition)

    def _bind_run_context(self, context: RunContext[AgentDepsT]) -> None:
        super()._bind_run_context(context)
        self._wrapped._bind_run_context(context)

    def _view_definitions(self) -> Iterable[NativeToolDefinition[Any]]:
        return tuple(self._transform(self._wrapped.definitions()))

    @property
    def changes_per_run_step(self) -> bool:
        return self._wrapped.changes_per_run_step

    @property
    def run_scoped_lifecycle(self) -> bool:
        return self._wrapped.run_scoped_lifecycle

    @property
    def dynamic_instruction_blocks(self) -> tuple[str, ...]:
        return self._wrapped.dynamic_instruction_blocks

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> NativeToolset[AgentDepsT]:
        wrapped = await self._wrapped.for_run(ctx)
        if wrapped is self._wrapped:
            self._bind_run_context(ctx)
            return self
        return _NativeToolsetView(
            self.id,
            wrapped,
            self._transform,
            requires_permission_gate=self._force_permission_gate,
            approval_policy=self._view_approval_policy,
            approval_mutating_only=self._view_approval_mutating_only,
            instructions=self._own_instruction_blocks,
        )

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> NativeToolset[AgentDepsT]:
        wrapped = await self._wrapped.for_run_step(ctx)
        if wrapped is self._wrapped:
            return self
        return _NativeToolsetView(
            self.id,
            wrapped,
            self._transform,
            requires_permission_gate=self._force_permission_gate,
            approval_policy=self._view_approval_policy,
            approval_mutating_only=self._view_approval_mutating_only,
            instructions=self._own_instruction_blocks,
        )

    async def __aenter__(self) -> NativeToolset[AgentDepsT]:
        await self._wrapped.__aenter__()
        return self

    async def __aexit__(self, *_exc: object) -> bool | None:
        return await self._wrapped.__aexit__(*_exc)


class _CombinedXmlToolset(XmlToolset[AgentDepsT]):
    def __init__(self, id: str, toolsets: Sequence[XmlToolset[AgentDepsT]]) -> None:
        self._toolsets = tuple(toolsets)
        self._exit_stack: AsyncExitStack | None = None
        super().__init__(
            id,
            self._combined_definitions,
            version=_combined_toolset_version(self._toolsets),
            requires_permission_gate=any(toolset.requires_permission_gate for toolset in self._toolsets),
            instructions=_dedupe_instruction_blocks(
                tuple(block for toolset in self._toolsets for block in toolset.static_instruction_blocks)
            ),
        )

    def _combined_definitions(self) -> Iterable[XmlToolDefinition[Any]]:
        for toolset in self._toolsets:
            yield from toolset.definitions()

    @property
    def changes_per_run_step(self) -> bool:
        return any(toolset.changes_per_run_step for toolset in self._toolsets)

    @property
    def run_scoped_lifecycle(self) -> bool:
        return any(toolset.run_scoped_lifecycle for toolset in self._toolsets)

    @property
    def dynamic_instruction_blocks(self) -> tuple[str, ...]:
        return _dedupe_instruction_blocks(
            tuple(block for toolset in self._toolsets for block in toolset.dynamic_instruction_blocks)
        )

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> XmlToolset[AgentDepsT]:
        active = await asyncio.gather(*(toolset.for_run(ctx) for toolset in self._toolsets))
        if all(new is old for new, old in zip(active, self._toolsets)):
            return self
        return _CombinedXmlToolset(self.id, active)

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> XmlToolset[AgentDepsT]:
        active = await asyncio.gather(*(toolset.for_run_step(ctx) for toolset in self._toolsets))
        if all(new is old for new, old in zip(active, self._toolsets)):
            return self
        return _CombinedXmlToolset(self.id, active)

    async def __aenter__(self) -> XmlToolset[AgentDepsT]:
        async with AsyncExitStack() as stack:
            for toolset in self._toolsets:
                if toolset.run_scoped_lifecycle:
                    await stack.enter_async_context(toolset)
            self._exit_stack = stack.pop_all()
        return self

    async def __aexit__(self, *_exc: object) -> bool | None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
        return None


class _CombinedNativeToolset(NativeToolset[AgentDepsT]):
    def __init__(self, id: str, toolsets: Sequence[NativeToolset[AgentDepsT]]) -> None:
        self._toolsets = tuple(toolsets)
        self._exit_stack: AsyncExitStack | None = None
        super().__init__(
            id,
            self._combined_definitions,
            version=_combined_toolset_version(self._toolsets),
            requires_permission_gate=any(toolset.requires_permission_gate for toolset in self._toolsets),
            instructions=_dedupe_instruction_blocks(
                tuple(block for toolset in self._toolsets for block in toolset.static_instruction_blocks)
            ),
        )

    def _combined_definitions(self) -> Iterable[NativeToolDefinition[Any]]:
        for toolset in self._toolsets:
            yield from toolset.definitions()

    @property
    def changes_per_run_step(self) -> bool:
        return any(toolset.changes_per_run_step for toolset in self._toolsets)

    @property
    def run_scoped_lifecycle(self) -> bool:
        return any(toolset.run_scoped_lifecycle for toolset in self._toolsets)

    @property
    def dynamic_instruction_blocks(self) -> tuple[str, ...]:
        return _dedupe_instruction_blocks(
            tuple(block for toolset in self._toolsets for block in toolset.dynamic_instruction_blocks)
        )

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> NativeToolset[AgentDepsT]:
        active = await asyncio.gather(*(toolset.for_run(ctx) for toolset in self._toolsets))
        if all(new is old for new, old in zip(active, self._toolsets)):
            return self
        return _CombinedNativeToolset(self.id, active)

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> NativeToolset[AgentDepsT]:
        active = await asyncio.gather(*(toolset.for_run_step(ctx) for toolset in self._toolsets))
        if all(new is old for new, old in zip(active, self._toolsets)):
            return self
        return _CombinedNativeToolset(self.id, active)

    async def __aenter__(self) -> NativeToolset[AgentDepsT]:
        async with AsyncExitStack() as stack:
            for toolset in self._toolsets:
                if toolset.run_scoped_lifecycle:
                    await stack.enter_async_context(toolset)
            self._exit_stack = stack.pop_all()
        return self

    async def __aexit__(self, *_exc: object) -> bool | None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
        return None


AnyToolset = XmlToolset[Any] | NativeToolset[Any]


def _combined_toolset_version(toolsets: Sequence[AnyToolset]) -> str:
    """Build a fixed-size deterministic version from ordered child identities."""

    canonical = json.dumps(
        [toolset.identity.to_payload() for toolset in toolsets],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"combined:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def toolset_manifest(toolsets: Sequence[AnyToolset]) -> ToolsetManifest:
    """Return the ordered durable identity manifest for Agent dependencies."""

    identities = tuple(toolset.identity for toolset in toolsets)
    if len({identity.id for identity in identities}) != len(identities):
        raise ToolsetConflictError("Toolset manifest contains duplicate ids")
    return identities


def _normalized_instruction_blocks(instructions: str | Iterable[str]) -> tuple[str, ...]:
    values = (instructions,) if isinstance(instructions, str) else tuple(instructions)
    normalized: list[str] = []
    for instruction in values:
        block = instruction.strip()
        if block:
            normalized.append(block)
    return _dedupe_instruction_blocks(tuple(normalized))


def _dedupe_instruction_blocks(instructions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(instructions))


def _validated_tool_name(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ToolsetCompositionError(f"{label} must not be empty")
    return normalized


def _validated_prefix(value: str) -> str:
    normalized = _validated_tool_name(value, label="Toolset prefix").strip("_")
    if not normalized:
        raise ToolsetCompositionError("Toolset prefix must not be empty")
    return normalized


def _validated_rename_mapping(mapping: Mapping[str, str]) -> dict[str, str]:
    normalized = {
        _validated_tool_name(source, label="Toolset rename source"): _validated_tool_name(
            target,
            label=f"Toolset rename target for {source!r}",
        )
        for source, target in mapping.items()
    }
    if len(set(normalized.values())) != len(normalized):
        raise ToolsetCompositionError("Toolset rename targets must be unique")
    return normalized


def _renamed_xml_definitions(
    toolset_id: str,
    originals: tuple[XmlToolDefinition[Any], ...],
    mapping: Mapping[str, str],
) -> Iterable[XmlToolDefinition[Any]]:
    available = {definition.name for definition in originals}
    unknown = sorted(mapping.keys() - available)
    if unknown:
        rendered = ", ".join(repr(name) for name in unknown)
        raise ToolsetCompositionError(f"Toolset {toolset_id!r} cannot rename unknown tools: {rendered}")
    for definition in originals:
        target = mapping.get(definition.name)
        yield definition.renamed(target) if target is not None else definition


def _renamed_native_definitions(
    toolset_id: str,
    originals: tuple[NativeToolDefinition[Any], ...],
    mapping: Mapping[str, str],
) -> Iterable[NativeToolDefinition[Any]]:
    available = {definition.name for definition in originals}
    unknown = sorted(mapping.keys() - available)
    if unknown:
        rendered = ", ".join(repr(name) for name in unknown)
        raise ToolsetCompositionError(f"Toolset {toolset_id!r} cannot rename unknown tools: {rendered}")
    for definition in originals:
        target = mapping.get(definition.name)
        yield definition.renamed(target) if target is not None else definition


def _prepared_definitions(
    toolset_id: str,
    protocol: CommandProtocol,
    originals: tuple[DefinitionT, ...],
    prepare: ToolDefinitionsPrepare[DefinitionT],
) -> Iterable[DefinitionT]:
    originals_by_name = {definition.name: definition for definition in originals}
    prepared = tuple(prepare(originals))
    for definition in prepared:
        original = originals_by_name.get(definition.name)
        if original is None:
            raise ToolsetCompositionError(
                f"Toolset preparation cannot add or rename tool {definition.name!r}; "
                "use rename() for model-facing names"
            )
        if definition.protocol is not protocol:
            raise ToolsetProtocolError(
                f"Toolset preparation for {toolset_id!r} produced a "
                f"{definition.protocol.value} definition in a {protocol.value} Toolset"
            )
        if definition.names != original.names:
            raise ToolsetCompositionError(
                f"Toolset preparation cannot change dispatch names for {definition.name!r}; " "use rename() or prefix()"
            )
        if definition.capability_factory is not original.capability_factory:
            raise ToolsetCompositionError(f"Toolset preparation cannot replace the capability for {definition.name!r}")
        if definition.approval_required != original.approval_required:
            raise ToolsetCompositionError(
                f"Toolset preparation cannot change approval policy for {definition.name!r}; " "use with_approval()"
            )
    owners: set[str] = set()
    for definition in prepared:
        for name in definition.names:
            if name in owners:
                raise ToolsetConflictError(f"tool {name!r} is declared more than once")
            owners.add(name)
    return prepared


def _validate_approval_policy(policy: object | None) -> None:
    if policy is not None and inspect.iscoroutinefunction(policy):
        raise ToolsetCompositionError(
            "Toolset approval policy must be synchronous; async approval belongs " "to the Runtime Permission gate"
        )


def _require_xml_definition(definition: ToolDefinition) -> XmlToolDefinition[Any]:
    if not isinstance(definition, XmlToolDefinition):
        raise ToolsetProtocolError("XML approval policy received a Native definition")
    return definition


def _require_native_definition(definition: ToolDefinition) -> NativeToolDefinition[Any]:
    if not isinstance(definition, NativeToolDefinition):
        raise ToolsetProtocolError("Native approval policy received an XML definition")
    return definition


def _validate_protocol_toolsets(protocol: CommandProtocol, toolsets: Sequence[object]) -> None:
    for toolset in toolsets:
        actual = getattr(toolset, "protocol", None)
        if actual is not protocol:
            rendered = actual.value if isinstance(actual, CommandProtocol) else repr(actual)
            raise ToolsetProtocolError(f"cannot compose {protocol.value} Toolset with {rendered} Toolset")


def validate_toolset_protocols(protocol: str | CommandProtocol, toolsets: Sequence[AnyToolset]) -> None:
    """Validate protocol tags and stable IDs without materializing definitions."""

    expected = CommandProtocol(protocol)
    _validate_protocol_toolsets(expected, toolsets)
    ids: set[str] = set()
    for toolset in toolsets:
        if toolset.identity.id != toolset.id:
            raise ToolsetCompositionError(f"Toolset identity id {toolset.identity.id!r} does not match {toolset.id!r}")
        if toolset.identity.protocol is not expected:
            raise ToolsetProtocolError(
                f"Toolset {toolset.id!r} identity is {toolset.identity.protocol.value} "
                f"but the Toolset is {expected.value}"
            )
        if toolset.id in ids:
            raise ToolsetConflictError(f"Toolset id {toolset.id!r} is declared more than once")
        ids.add(toolset.id)


def validate_toolset_composition(
    protocol: str | CommandProtocol,
    toolsets: Sequence[AnyToolset],
) -> None:
    """Materialize definitions and reject cross-Toolset dispatch conflicts."""

    materialize_toolset_index(protocol, toolsets)


def materialize_toolset_index(
    protocol: str | CommandProtocol,
    toolsets: Sequence[AnyToolset],
) -> dict[str, tuple[AnyToolset, ToolDefinition]]:
    """Build one validated dispatch snapshot from the current Toolset views."""

    validate_toolset_protocols(protocol, toolsets)
    owners: dict[str, str] = {}
    resolved: dict[str, tuple[AnyToolset, ToolDefinition]] = {}
    for toolset in toolsets:
        for definition in toolset.definitions():
            for name in definition.names:
                previous = owners.get(name)
                if previous is not None:
                    raise ToolsetConflictError(
                        f"tool {name!r} is provided by both Toolset {previous!r} " f"and Toolset {toolset.id!r}"
                    )
                owners[name] = toolset.id
                resolved[name] = (toolset, definition)
    return resolved


def resolve_tool(
    toolsets: tuple[AnyToolset, ...],
    name: str,
) -> tuple[AnyToolset, ToolDefinition] | None:
    """Resolve a declared name from exactly one same-protocol Toolset."""

    if not toolsets:
        return None
    return materialize_toolset_index(toolsets[0].protocol, toolsets).get(name)


__all__ = [
    "AnyToolset",
    "DefinitionSource",
    "NativeToolset",
    "NativeApprovalPolicy",
    "ToolFilter",
    "ToolDefinitionsPrepare",
    "Toolset",
    "ToolsetPolicy",
    "ToolsetCompositionError",
    "ToolsetConflictError",
    "XmlToolset",
    "XmlApprovalPolicy",
    "materialize_toolset_index",
    "resolve_tool",
    "toolset_manifest",
    "validate_toolset_composition",
    "validate_toolset_protocols",
]
