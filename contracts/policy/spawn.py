"""Typed data for the child-Agent spawn admission domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SpawnIntent:
    parent_path: str
    child_depth: int
    max_depth: Optional[int] = None
    fleet_cost_usd: float = 0.0
    max_cost_usd: Optional[float] = None
    fleet_total_tokens: int = 0
    max_total_tokens: Optional[int] = None
    agent_role: str = ""
    nickname: str = ""


@dataclass(frozen=True)
class SpawnPolicyContribution:
    """A monotonic extension contribution: narrow ceilings or deny."""

    allowed: bool = True
    max_depth: Optional[int] = None
    max_cost_usd: Optional[float] = None
    max_total_tokens: Optional[int] = None
    reason: str = ""

    @classmethod
    def deny(cls, reason: str) -> "SpawnPolicyContribution":
        return cls(allowed=False, reason=reason)


@dataclass(frozen=True)
class SpawnPolicyTraceEntry:
    step: str
    disposition: str
    detail: str = ""


@dataclass(frozen=True)
class SpawnDecision:
    accepted: bool
    reason: str = ""
    trace: tuple[SpawnPolicyTraceEntry, ...] = ()


__all__ = [
    "SpawnDecision",
    "SpawnIntent",
    "SpawnPolicyContribution",
    "SpawnPolicyTraceEntry",
]
