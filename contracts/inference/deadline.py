from datetime import datetime, timezone
from time import monotonic
from typing import Literal

from pydantic import Field, model_validator

from mote.contracts.inference.base import FrozenContract


class CrossProcessDeadline(FrozenContract):
    schema_version: Literal[1] = 1
    deadline_utc: datetime
    remaining_seconds_at_send: float = Field(gt=0)
    sent_at_utc: datetime

    @model_validator(mode="after")
    def _timestamps_are_aware_and_ordered(self) -> "CrossProcessDeadline":
        if self.deadline_utc.utcoffset() is None or self.sent_at_utc.utcoffset() is None:
            raise ValueError("cross-process deadline timestamps must be timezone-aware")
        if self.sent_at_utc >= self.deadline_utc:
            raise ValueError("deadline must be after sent_at")
        return self

    def to_local_deadline(
        self,
        *,
        received_at_utc: datetime | None = None,
        local_monotonic: float | None = None,
        clock_skew_guard_seconds: float = 0.0,
    ) -> float:
        received = received_at_utc or datetime.now(timezone.utc)
        if received.utcoffset() is None:
            raise ValueError("received_at_utc must be timezone-aware")
        transport_elapsed = max((received - self.sent_at_utc).total_seconds(), 0.0)
        by_remaining = self.remaining_seconds_at_send - transport_elapsed
        by_utc = (self.deadline_utc - received).total_seconds() - clock_skew_guard_seconds
        safe_remaining = max(min(by_remaining, by_utc), 0.0)
        return (monotonic() if local_monotonic is None else local_monotonic) + safe_remaining
