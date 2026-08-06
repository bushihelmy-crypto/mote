"""Durable authority to trigger process-local cancellation for one run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RunInterruptPermit:
    run_id: str
    owner_id: str
    incarnation_id: str
    fencing_token: int
    interrupted_at: datetime

    def __post_init__(self) -> None:
        if not self.run_id or not self.owner_id or not self.incarnation_id:
            raise ValueError("interrupt permit identity must be non-empty")
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise ValueError("interrupt permit fence must be positive")
        if self.interrupted_at.tzinfo is None:
            raise ValueError("interrupt permit instant must be timezone-aware")


__all__ = ["RunInterruptPermit"]
