"""Narrow extension seams for CompactionPolicy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mote.contracts.conversation.compaction_policy import (
    CompactionDecision,
    CompactionIntent,
    CompactionPolicyContribution,
)


class CompactionPolicy(Protocol):
    async def process(self, intent: CompactionIntent) -> CompactionDecision:
        ...


class CompactionPolicyExtension(Protocol):
    async def evaluate(self, intent: CompactionIntent) -> CompactionPolicyContribution:
        ...


CompactionPolicyExtensionFactory = Callable[[], CompactionPolicyExtension]


@dataclass(frozen=True)
class CompactionPolicyExtensionSpec:
    identity: str
    factory: CompactionPolicyExtensionFactory
    timeout: float = 5.0


__all__ = [
    "CompactionPolicy",
    "CompactionPolicyExtension",
    "CompactionPolicyExtensionFactory",
    "CompactionPolicyExtensionSpec",
]
