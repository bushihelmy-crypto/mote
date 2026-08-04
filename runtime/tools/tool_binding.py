"""Runtime binding between one wire definition and one capability instance."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Awaitable, ClassVar, Protocol, cast

from mote.contracts.authorization import PermissionDecision
from mote.contracts.events.envelope import JsonValue
from mote.contracts.tool.arguments import ToolArguments
from mote.contracts.tool.effects import ToolEffect
from mote.runtime.tools.definition_compiler import CompiledToolDefinition, compile_tool_definition
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition


@dataclass(frozen=True, slots=True)
class BoundApprovalPolicy:
    identity: str
    evaluate: Callable[[ToolArguments], bool]


class ToolCapability(Protocol):
    reconstructable: ClassVar[bool]
    graph_excluded: ClassVar[bool]
    max_result_size_chars: ClassVar[int]

    def call(self, **kwargs: Any) -> Any: ...
    def resolve_effect(self) -> ToolEffect: ...
    def resolve_effect_for(self, args: dict[str, Any]) -> ToolEffect: ...
    def mutates_filesystem_for(self, args: dict[str, Any]) -> bool: ...
    def permission_targets(self, args: dict[str, Any]) -> list[str]: ...
    def permission_segments(self, args: dict[str, Any]) -> list[str] | None: ...
    def can_resume_started_call(self, call_id: str) -> bool: ...
    def cleanup_session(self, session_id: str) -> Awaitable[None] | None: ...
    def check_permissions(self, args: dict[str, Any]) -> PermissionDecision | None: ...


@dataclass(frozen=True, slots=True, init=False)
class ExecutableToolBinding:
    """Protocol definition plus the capability run by the shared control plane."""

    definition: XmlToolDefinition | NativeToolDefinition
    _capability: ToolCapability
    _approval_policy: BoundApprovalPolicy | None
    name: str
    _compiled_definition: CompiledToolDefinition
    _invoke: Callable[[dict[str, JsonValue]], Awaitable[object]]
    _cleanup: Callable[[str], Awaitable[None]]

    def __init__(
        self,
        definition: XmlToolDefinition | NativeToolDefinition,
        capability: ToolCapability,
        approval_policy: BoundApprovalPolicy | None = None,
    ) -> None:
        object.__setattr__(self, "definition", definition)
        object.__setattr__(self, "_capability", capability)
        object.__setattr__(self, "_approval_policy", approval_policy)
        object.__setattr__(self, "name", definition.name)
        object.__setattr__(self, "_invoke", self._bind_invoker(capability))
        object.__setattr__(self, "_cleanup", self._bind_cleanup(capability))
        object.__setattr__(
            self,
            "_compiled_definition",
            compile_tool_definition(
                definition,
                capability,
                approval_identity=(
                    approval_policy.identity
                    if approval_policy is not None
                    else ("definition-required" if definition.approval_required else "none")
                ),
            ),
        )

    @staticmethod
    def _bind_invoker(capability: ToolCapability) -> Callable[[dict[str, JsonValue]], Awaitable[object]]:
        if inspect.iscoroutinefunction(capability.call):

            async def invoke_async(arguments: dict[str, JsonValue]) -> object:
                return await capability.call(**arguments)

            return invoke_async

        async def invoke_sync(arguments: dict[str, JsonValue]) -> object:
            return capability.call(**arguments)

        return invoke_sync

    @staticmethod
    def _bind_cleanup(capability: ToolCapability) -> Callable[[str], Awaitable[None]]:
        if inspect.iscoroutinefunction(capability.cleanup_session):
            async_cleanup = cast(Callable[[str], Awaitable[None]], capability.cleanup_session)

            async def cleanup_async(session_id: str) -> None:
                await async_cleanup(session_id)

            return cleanup_async

        async def cleanup_sync(session_id: str) -> None:
            capability.cleanup_session(session_id)

        return cleanup_sync

    @property
    def compiled_definition(self) -> CompiledToolDefinition:
        return self._compiled_definition

    @property
    def semantic_identity(self) -> str:
        return self._compiled_definition.semantic_identity

    @property
    def reconstructable(self) -> bool:
        return bool(self._capability.reconstructable)

    @property
    def graph_excluded(self) -> bool:
        return bool(self._capability.graph_excluded)

    @property
    def max_result_size_chars(self) -> int:
        return int(self._capability.max_result_size_chars)

    def resolve_effect(self):
        return self._capability.resolve_effect()

    def resolve_effect_for(self, args: dict[str, JsonValue]) -> ToolEffect:
        return self._capability.resolve_effect_for(args)

    def mutates_filesystem_for(self, args: dict[str, JsonValue]) -> bool:
        return self._capability.mutates_filesystem_for(args)

    def permission_targets(self, args: dict[str, JsonValue]) -> list[str]:
        return self._capability.permission_targets(args)

    def permission_segments(self, args: dict[str, JsonValue]) -> list[str] | None:
        return self._capability.permission_segments(args)

    def can_resume_started_call(self, call_id: str) -> bool:
        return bool(self._capability.can_resume_started_call(call_id))

    async def cleanup_session(self, session_id: str) -> None:
        await self._cleanup(session_id)

    def check_permissions(self, args: dict[str, JsonValue]) -> PermissionDecision | None:
        decision = self._capability.check_permissions(args)
        if decision is not None and decision.behavior == "deny":
            return decision
        selected = False
        if self._approval_policy is not None:
            selected = self._approval_policy.evaluate(args)
            if type(selected) is not bool:
                raise TypeError("Toolset approval policy must return bool")
            if not selected and not self.definition.approval_required:
                return PermissionDecision.allow(
                    "toolset",
                    f"Toolset policy allows '{self.name}'.",
                )
        if self.definition.approval_required or selected:
            return PermissionDecision.ask(
                "toolset",
                f"Toolset policy requires approval for '{self.name}'.",
            )
        return decision

    async def call(self, **kwargs: JsonValue) -> object:
        arguments = self.definition.argument_decoder(kwargs)
        return await self._invoke(arguments)


__all__ = ["BoundApprovalPolicy", "ExecutableToolBinding"]
