"""Narrow extension seams for post-flow run completion policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mote.contracts.policy.run_completion import (
    RunCompletionDecision,
    RunCompletionIntent,
    RunCompletionPolicyContribution,
)


class RunCompletionPolicy(Protocol):
    async def process(self, intent: RunCompletionIntent) -> RunCompletionDecision:
        ...


class RunCompletionPolicyExtension(Protocol):
    async def evaluate(self, intent: RunCompletionIntent) -> RunCompletionPolicyContribution:
        ...


RunCompletionPolicyExtensionFactory = Callable[[], RunCompletionPolicyExtension]


@dataclass(frozen=True)
class RunCompletionPolicyExtensionSpec:
    identity: str
    factory: RunCompletionPolicyExtensionFactory
    timeout: float = 5.0


__all__ = [
    "RunCompletionPolicy",
    "RunCompletionPolicyExtension",
    "RunCompletionPolicyExtensionFactory",
    "RunCompletionPolicyExtensionSpec",
]
