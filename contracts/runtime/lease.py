"""Stable values for fenced Runtime resource ownership."""

import math
from dataclasses import dataclass

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant


@dataclass(frozen=True, slots=True)
class RuntimeLease:
    subject: str
    owner_id: str
    fencing_token: int
    expires_at: float
    expires_at_instant: AbsoluteInstant | None = None

    def __post_init__(self) -> None:
        if type(self.subject) is not str or not self.subject:
            raise ValueError("runtime lease subject is invalid")
        if type(self.owner_id) is not str:
            raise ValueError("runtime lease owner is invalid")
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise ValueError("runtime lease fencing token is invalid")
        if type(self.expires_at) not in {int, float} or not math.isfinite(self.expires_at) or self.expires_at < 0:
            raise ValueError("runtime lease expiry is invalid")
        if self.expires_at_instant is not None:
            if not isinstance(self.expires_at_instant, AbsoluteInstant):
                raise TypeError("runtime lease durable expiry must be an AbsoluteInstant")
            self.expires_at_instant.require_clock(UNIX_UTC_CLOCK)

    def durable_expiry(self) -> AbsoluteInstant:
        if self.expires_at_instant is not None:
            return self.expires_at_instant
        return AbsoluteInstant(
            1,
            UNIX_UTC_CLOCK,
            int(self.expires_at * 1_000_000_000),
        )


@dataclass(frozen=True, slots=True)
class RuntimeLeasePolicy:
    ttl_seconds: float = 30.0
    renew_interval_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        if not 0 < self.renew_interval_seconds < self.ttl_seconds:
            raise ValueError("lease renew_interval_seconds must be positive and less than ttl_seconds")


__all__ = ["RuntimeLease", "RuntimeLeasePolicy"]
