"""Typed deny-only extension base for ToolCallPolicy."""
from __future__ import annotations

from abc import ABC, abstractmethod

from mote.contracts.permissions import PermissionFacts
from mote.contracts.policy.tool import ToolCallInspection, ToolCallIntent


class ToolCallInspector(ABC):
    """Base for policy extensions installed by an explicit manifest entry.

    The slot can only allow the pipeline to continue or deny the current call.
    It cannot rewrite arguments, force an allow, or replace core permission and
    sandbox checks.
    """

    @abstractmethod
    async def inspect(
        self,
        intent: ToolCallIntent,
        facts: PermissionFacts,
    ) -> ToolCallInspection:
        """Inspect the hook-rewritten intent and its recomputed facts."""
        ...


__all__ = ["ToolCallInspector"]
