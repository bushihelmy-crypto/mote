"""Durable Agent delivery lifecycle contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AgentDeliveryState(str, Enum):
    ACCEPTED = "accepted"
    CLAIMED = "claimed"
    ACKED = "acked"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class AgentDeliveryRecord:
    delivery_id: str
    target_agent_id: str
    target_generation: int
    mode: str
    message_payload: str
    state: AgentDeliveryState
    revision: int
    fencing_token: int
    reason: str = ""


__all__ = ["AgentDeliveryRecord", "AgentDeliveryState"]
