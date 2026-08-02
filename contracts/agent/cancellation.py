"""Typed subtree cancellation commands and per-Agent settlement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class AgentCancellationDisposition(StrEnum):
    SETTLED = "settled"
    ALREADY_TERMINAL = "already_terminal"
    OWNER_LOST = "owner_lost"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class AgentCancellationCommand:
    root_agent_id: str
    subtree_agent_id: str
    target_agent_id: str
    lineage_revision: int
    cancellation_epoch: int


@dataclass(frozen=True, slots=True)
class AgentCancellationReceipt:
    target_agent_id: str
    cancellation_epoch: int
    disposition: AgentCancellationDisposition
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SubtreeCancellationReceipt:
    root_agent_id: str
    subtree_agent_id: str
    lineage_revision: int
    cancellation_epoch: int
    settlements: tuple[AgentCancellationReceipt, ...]


class AgentCancellationPort(Protocol):
    async def cancel_agent_scope(self, command: AgentCancellationCommand) -> AgentCancellationReceipt: ...


__all__ = [
    "AgentCancellationCommand",
    "AgentCancellationDisposition",
    "AgentCancellationPort",
    "AgentCancellationReceipt",
    "SubtreeCancellationReceipt",
]
