"""Stable values for fenced Runtime resource ownership."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeLease:
    subject: str
    owner_id: str
    fencing_token: int
    expires_at: float


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
