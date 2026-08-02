"""Immutable Agent governance budget requests and receipts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mote.contracts.inference.governance import BudgetReservation, UsageSettlement


class AgentBudgetDisposition(StrEnum):
    RESERVED = "reserved"
    REJECTED_POLICY = "rejected_policy"
    REJECTED_BUDGET = "rejected_budget"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class AgentBudgetPolicy:
    max_tokens: int
    max_cost_micro_usd: int
    max_depth: int
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if min(self.max_tokens, self.max_cost_micro_usd, self.max_depth) < 0:
            raise ValueError("Agent budget limits cannot be negative")
        if any(not capability for capability in self.capabilities):
            raise ValueError("Agent capabilities must have stable identities")

    def narrowed_by(self, extension: "AgentBudgetPolicy") -> "AgentBudgetPolicy":
        if not extension.capabilities.issubset(self.capabilities):
            raise ValueError("budget extension attempted to expand capabilities")
        if (
            extension.max_tokens > self.max_tokens
            or extension.max_cost_micro_usd > self.max_cost_micro_usd
            or extension.max_depth > self.max_depth
        ):
            raise ValueError("budget extension attempted to expand limits")
        return extension


@dataclass(frozen=True, slots=True)
class AgentBudgetRequest:
    request_id: str
    root_id: str
    subtree_id: str
    agent_id: str
    requested_tokens: int | None
    requested_cost_micro_usd: int | None
    child_depth: int
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if any(not value for value in (self.request_id, self.root_id, self.subtree_id, self.agent_id)):
            raise ValueError("Agent budget request identities cannot be empty")
        if self.child_depth < 0:
            raise ValueError("Agent child depth cannot be negative")
        for value in (self.requested_tokens, self.requested_cost_micro_usd):
            if value is not None and value < 0:
                raise ValueError("Agent requested usage cannot be negative")


@dataclass(frozen=True, slots=True)
class AgentBudgetReservationReceipt:
    request_id: str
    disposition: AgentBudgetDisposition
    reservations: tuple[BudgetReservation, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AgentBudgetSettlementReceipt:
    request_id: str
    settlements: tuple[UsageSettlement, ...]


__all__ = [
    "AgentBudgetDisposition",
    "AgentBudgetPolicy",
    "AgentBudgetRequest",
    "AgentBudgetReservationReceipt",
    "AgentBudgetSettlementReceipt",
]
