"""Narrow PromptPolicy seams consumed by Role and composition roots."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mote.contracts.policy.prompt import PromptDecision, PromptIntent, PromptPolicyContribution


class PromptPolicy(Protocol):
    async def process(self, intent: PromptIntent) -> PromptDecision:
        ...


class PromptPolicyExtension(Protocol):
    """A safe-view extension that may enrich context or deny the prompt."""

    async def evaluate(self, intent: PromptIntent) -> PromptPolicyContribution:
        ...


PromptPolicyExtensionFactory = Callable[[], PromptPolicyExtension]


@dataclass(frozen=True)
class PromptPolicyExtensionSpec:
    """Host-owned manifest entry for one per-Role Prompt extension."""

    identity: str
    factory: PromptPolicyExtensionFactory
    timeout: float = 5.0


__all__ = [
    "PromptPolicy",
    "PromptPolicyExtension",
    "PromptPolicyExtensionFactory",
    "PromptPolicyExtensionSpec",
]
