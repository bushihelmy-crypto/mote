"""Narrow extension seams for SpawnAdmissionPolicy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mote.contracts.policy.spawn import SpawnDecision, SpawnIntent, SpawnPolicyContribution


class SpawnAdmissionPolicy(Protocol):
    async def process(self, intent: SpawnIntent) -> SpawnDecision:
        ...


class SpawnPolicyExtension(Protocol):
    async def evaluate(self, intent: SpawnIntent) -> SpawnPolicyContribution:
        ...


SpawnPolicyExtensionFactory = Callable[[], SpawnPolicyExtension]


@dataclass(frozen=True)
class SpawnPolicyExtensionSpec:
    identity: str
    factory: SpawnPolicyExtensionFactory
    timeout: float = 5.0


__all__ = [
    "SpawnAdmissionPolicy",
    "SpawnPolicyExtension",
    "SpawnPolicyExtensionFactory",
    "SpawnPolicyExtensionSpec",
]
