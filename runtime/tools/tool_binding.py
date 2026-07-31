"""Runtime binding between one wire definition and one capability instance."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mote.contracts.authorization import PermissionDecision
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition


@dataclass(frozen=True, slots=True)
class BoundApprovalPolicy:
    evaluate: Callable[[Mapping[str, Any]], bool]


class BoundTool:
    """Protocol definition plus the capability run by the shared control plane."""

    def __init__(
        self,
        definition: XmlToolDefinition[Any] | NativeToolDefinition[Any],
        capability: Any,
        approval_policy: BoundApprovalPolicy | None = None,
    ) -> None:
        self.definition = definition
        self._capability = capability
        self._approval_policy = approval_policy
        self.name = definition.name

    @property
    def wrapped_tool(self) -> Any:
        return self._capability

    @property
    def validation_callable(self) -> Any:
        """The capability signature used for pre-dispatch argument validation."""

        return self._capability.call

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

    def resolve_effect_for(self, args: dict[str, Any]):
        return self._capability.resolve_effect_for(args)

    def mutates_filesystem_for(self, args: dict[str, Any]) -> bool:
        return self._capability.mutates_filesystem_for(args)

    def permission_targets(self, args: dict[str, Any]) -> list[str]:
        return self._capability.permission_targets(args)

    def check_permissions(self, args: dict[str, Any]) -> PermissionDecision | None:
        decision = self._capability.check_permissions(args)
        if decision is not None and decision.behavior == "deny":
            return decision
        selected = False
        if self._approval_policy is not None:
            selected = self._approval_policy.evaluate(args)
            if inspect.isawaitable(selected):
                if inspect.iscoroutine(selected):
                    selected.close()
                raise TypeError("Toolset approval policy must return bool, not an awaitable")
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

    async def call(self, **kwargs: Any) -> Any:
        arguments = self.definition.argument_decoder(kwargs)
        result = self._capability.call(**arguments)
        return await result if inspect.isawaitable(result) else result


__all__ = ["BoundApprovalPolicy", "BoundTool"]
