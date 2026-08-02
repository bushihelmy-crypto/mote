"""Atomic process lifecycle for logical Agent residency incarnations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ResidentLifecyclePhase(StrEnum):
    ACTIVE = "active"
    EVICTING = "evicting"
    DRAINING = "draining"
    EVICTION_RETRY = "eviction_retry"
    EVICTED = "evicted"
    REHYDRATING = "rehydrating"
    LOST = "lost"
    TERMINATING = "terminating"
    TERMINAL = "terminal"
    TOMBSTONED = "tombstoned"
    PURGED = "purged"


class ResidentTransitionDisposition(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    STALE = "stale"
    PINNED_DRAINING = "pinned_draining"
    FAILED_RETRYABLE = "failed_retryable"
    REJECTED_GUARD = "rejected_guard"


@dataclass(frozen=True, slots=True)
class ResidentPurgeAuthorization:
    retention_expired: bool
    delivery_settled: bool
    effects_settled: bool
    pins_released: bool
    legal_hold: bool = False

    @property
    def permits_purge(self) -> bool:
        return (
            self.retention_expired
            and self.delivery_settled
            and self.effects_settled
            and self.pins_released
            and not self.legal_hold
        )


@dataclass(frozen=True, slots=True)
class ResidentLifecycleSnapshot:
    agent_id: str
    incarnation_generation: int
    revision: int
    phase: ResidentLifecyclePhase


@dataclass(frozen=True, slots=True)
class ResidentTransitionClaim:
    before: ResidentLifecycleSnapshot
    claimed: ResidentLifecycleSnapshot


@dataclass(frozen=True, slots=True)
class ResidentTransitionReceipt:
    command: str
    disposition: ResidentTransitionDisposition
    snapshot: ResidentLifecycleSnapshot


__all__ = [
    "ResidentLifecyclePhase",
    "ResidentLifecycleSnapshot",
    "ResidentPurgeAuthorization",
    "ResidentTransitionClaim",
    "ResidentTransitionDisposition",
    "ResidentTransitionReceipt",
]
