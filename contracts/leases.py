"""Stable value objects for distributed run ownership."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Lease:
    """One ownership epoch for an arbitrary fenced subject."""

    subject: str
    owner_id: str
    fencing_token: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class LeasePolicy:
    """Heartbeat policy shared by run, runtime, artifact and output leases."""

    ttl_seconds: float = 30.0
    renew_interval_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        if not 0 < self.renew_interval_seconds < self.ttl_seconds:
            raise ValueError("lease renew_interval_seconds must be positive and less than ttl_seconds")


@dataclass(frozen=True, slots=True)
class RunLease:
    run_id: str
    owner_id: str
    fencing_token: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class RunLeasePolicy:
    ttl_seconds: float = 30.0
    renew_interval_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        if not 0 < self.renew_interval_seconds < self.ttl_seconds:
            raise ValueError("lease renew_interval_seconds must be positive and less than ttl_seconds")


__all__ = ["Lease", "LeasePolicy", "RunLease", "RunLeasePolicy"]
