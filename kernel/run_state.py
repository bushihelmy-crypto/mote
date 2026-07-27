"""Transient state owned by one Kernel Agent run."""

from __future__ import annotations

from dataclasses import dataclass, field

from mote.contracts.think import ThinkResult


@dataclass
class AgentRunState:
    """Non-durable signals shared by Flow and tool-driven stop capabilities."""

    active: bool = False
    last_think_result: ThinkResult = field(default_factory=ThinkResult)


__all__ = ["AgentRunState"]
