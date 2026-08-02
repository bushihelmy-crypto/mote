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

    def __post_init__(self) -> None:
        for name in ("trigger_id", "source_id", "target", "content"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"automation trigger {name} is invalid")
        if type(self.scheduled_at_ms) is not int or type(self.fired_at_ms) is not int:
            raise ValueError("automation trigger instants must be integers")
        if self.fired_at_ms < self.scheduled_at_ms:
            raise ValueError("automation trigger fired before its occurrence")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("automation trigger attempt is invalid")


class TriggerDisposition(str, Enum):
    ACCEPTED = "accepted"
    DEFERRED = "deferred"
    REJECTED = "rejected"


@dataclass(frozen=True)
class TriggerReceipt:
    disposition: TriggerDisposition
    receipt_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.disposition is TriggerDisposition.ACCEPTED and not self.receipt_id:
            raise ValueError("accepted trigger receipt requires receipt_id")
        for name in ("receipt_id", "reason"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"trigger receipt {name} must be non-empty when present")


class TriggerSink(Protocol):
    def dispatch(self, trigger: AutomationTrigger) -> TriggerReceipt: ...


__all__ = [
    "AutomationTrigger",
    "TriggerDisposition",
    "TriggerReceipt",
    "TriggerSink",
]
