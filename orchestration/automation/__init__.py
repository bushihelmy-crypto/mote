"""Stable automation trigger contract."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


@dataclass(frozen=True)
class AutomationTrigger:
    trigger_id: str
    source_id: str
    target: str
    content: str
    scheduled_at_ms: int
    fired_at_ms: int
    attempt: int = 1


class TriggerDisposition(str, Enum):
    ACCEPTED = "accepted"
    DEFERRED = "deferred"
    REJECTED = "rejected"


@dataclass(frozen=True)
class TriggerReceipt:
    disposition: TriggerDisposition
    receipt_id: str | None = None
    reason: str | None = None


class TriggerSink(Protocol):
    def dispatch(self, trigger: AutomationTrigger) -> TriggerReceipt:
        ...


__all__ = [
    "AutomationTrigger",
    "TriggerDisposition",
    "TriggerReceipt",
    "TriggerSink",
]
