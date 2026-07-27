"""Runtime binding between one wire definition and one capability instance."""

from __future__ import annotations

import inspect
from types import MappingProxyType
from typing import Any

from mote.contracts.permissions import PermissionDecision
from mote.kernel.tools.definitions import NativeToolDefinition, XmlToolDefinition
from mote.runtime.run_context import current_run_context


class BoundTool:
    """Protocol definition plus the capability run by the shared control plane."""

    def __init__(
        self,
        definition: XmlToolDefinition[Any] | NativeToolDefinition[Any],
        capability: Any,
    ) -> None:
        self.definition = definition
        self._capability = capability
        self.name = definition.name

    @property
    def wrapped_tool(self) -> Any:
        return self._capability

    @property
    def validation_callable(self) -> Any:
        """The capability signature used for pre-dispatch argument validation."""

        return self._capability.call

    def check_permissions(self, args: dict) -> PermissionDecision | None:
        decision = self._capability.check_permissions(args)
        if decision is not None and decision.behavior == "deny":
            return decision
        predicate = self.definition.approval_predicate
        context = current_run_context() if predicate is not None else None
        selected = False
        if predicate is not None and context is not None:
            selected = predicate(context, MappingProxyType(dict(args)))
            if inspect.isawaitable(selected):
                if inspect.iscoroutine(selected):
                    selected.close()
                raise TypeError("Toolset approval policy must return bool, not an awaitable")
            if not isinstance(selected, bool):
                raise TypeError("Toolset approval policy must return bool")
        if self.definition.approval_required or (predicate is not None and (context is None or selected)):
            return PermissionDecision.ask(
                "toolset",
                (
                    f"Toolset policy requires approval for '{self.name}'."
                    if context is not None or predicate is None
                    else f"Toolset policy for '{self.name}' requires an active RunContext."
                ),
            )
        return decision

    async def call(self, **kwargs: Any) -> Any:
        arguments = self.definition.argument_decoder(kwargs)
        result = self._capability.call(**arguments)
        return await result if inspect.isawaitable(result) else result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._capability, name)


__all__ = ["BoundTool"]
