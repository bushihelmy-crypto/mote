"""Narrow ToolCall policy seam consumed by the tool execution pipeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from mote.contracts.permissions import PermissionFacts
from mote.contracts.policy.tool import (
    ToolCallDecision,
    ToolCallInspection,
    ToolCallIntent,
    ToolResultIntent,
    ToolResultPresentation,
)

PermissionFactsResolver = Callable[[dict[str, Any]], PermissionFacts]


class ToolCallPolicy(Protocol):
    async def authorize(
        self,
        intent: ToolCallIntent,
        resolve_permission_facts: PermissionFactsResolver,
    ) -> ToolCallDecision:
        ...


class ToolResultPolicy(Protocol):
    async def present(self, intent: ToolResultIntent) -> ToolResultPresentation:
        ...


class ToolCallPolicyExtension(Protocol):
    """A typed, deny-only extension installed into ToolCallPolicy."""

    async def inspect(
        self,
        intent: ToolCallIntent,
        facts: PermissionFacts,
    ) -> ToolCallInspection:
        ...


ToolCallPolicyExtensionFactory = Callable[[], ToolCallPolicyExtension]


@dataclass(frozen=True)
class ToolCallPolicyExtensionSpec:
    """Host-owned manifest entry for a per-Role ToolCall extension."""

    identity: str
    factory: ToolCallPolicyExtensionFactory
    timeout: float = 5.0


__all__ = [
    "PermissionFactsResolver",
    "ToolCallPolicy",
    "ToolCallPolicyExtension",
    "ToolCallPolicyExtensionFactory",
    "ToolCallPolicyExtensionSpec",
    "ToolResultPolicy",
]
