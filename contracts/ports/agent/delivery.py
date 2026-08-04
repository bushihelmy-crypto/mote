"""Canonical Product-facing command surface for durable Agent delivery."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class AgentDeliverySourceKind(StrEnum):
    AUTOMATION = "automation"
    WORKFLOW = "workflow"


class AgentDeliveryCommandDisposition(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_SETTLED = "already_settled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AgentDeliveryCommand:
    source_kind: AgentDeliverySourceKind
    source_id: str
    target_agent_id: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, AgentDeliverySourceKind):
            raise TypeError("Agent delivery source kind is invalid")
        if not self.source_id or not self.target_agent_id or type(self.content) is not str:
            raise ValueError("Agent delivery command is invalid")


@dataclass(frozen=True, slots=True)
class AgentDeliveryCommandReceipt:
    disposition: AgentDeliveryCommandDisposition
    delivery_id: str
    reason: str = ""


class AgentDeliveryPort(Protocol):
    def dispatch(self, command: AgentDeliveryCommand) -> AgentDeliveryCommandReceipt: ...


__all__ = [
    "AgentDeliveryCommand",
    "AgentDeliveryCommandDisposition",
    "AgentDeliveryCommandReceipt",
    "AgentDeliveryPort",
    "AgentDeliverySourceKind",
]
