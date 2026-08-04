"""Authoritative Agent delivery v2 lifecycle contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AgentDeliveryState(StrEnum):
    ACCEPTED = "accepted"
    BOUND_TO_TURN = "bound_to_turn"
    ACKED = "acked"
    DEAD_LETTER = "dead_letter"
    OWNER_ACTION_REQUIRED = "owner_action_required"
    TOMBSTONED = "tombstoned"


@dataclass(frozen=True)
class AgentDeliveryRecord:
    delivery_id: str
    target_agent_id: str
    target_generation: int
    mode: str
    message_payload: str
    payload_digest: str
    state: AgentDeliveryState
    revision: int
    fencing_token: int
    turn_request_id: str | None = None
    reason: str = ""
    accepted_at: datetime | None = None
    terminal_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("delivery_id", "target_agent_id", "mode", "message_payload", "payload_digest"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"Agent delivery {name} is invalid")
        if type(self.target_generation) is not int or self.target_generation < 0:
            raise ValueError("Agent delivery target generation is invalid")
        if not isinstance(self.state, AgentDeliveryState):
            raise TypeError("Agent delivery state is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Agent delivery revision is invalid")
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise ValueError("Agent delivery fencing token is invalid")
        if self.turn_request_id is not None and (type(self.turn_request_id) is not str or not self.turn_request_id):
            raise ValueError("Agent delivery turn binding is invalid")
        if self.state is AgentDeliveryState.BOUND_TO_TURN and self.turn_request_id is None:
            raise ValueError("bound Agent delivery requires a turn identity")
        if self.state is AgentDeliveryState.ACCEPTED and self.turn_request_id is not None:
            raise ValueError("unbound Agent delivery cannot carry a turn identity")
        if type(self.reason) is not str:
            raise TypeError("Agent delivery reason is invalid")
        for instant in (self.accepted_at, self.terminal_at):
            if instant is not None and (instant.tzinfo is None or instant.utcoffset() is None):
                raise ValueError("delivery timestamp must be timezone-aware")
        if self.accepted_at is None:
            raise ValueError("delivery accepted_at is required")
        if (
            self.state in {AgentDeliveryState.ACKED, AgentDeliveryState.DEAD_LETTER, AgentDeliveryState.TOMBSTONED}
            and self.terminal_at is None
        ):
            raise ValueError("terminal delivery requires terminal_at")


__all__ = ["AgentDeliveryRecord", "AgentDeliveryState"]
