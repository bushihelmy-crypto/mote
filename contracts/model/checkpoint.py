"""Versioned Product-selected bounds for durable ModelCall state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from mote.contracts.model.failover import AttemptBudget


class ModelCheckpointAdmissionDisposition(StrEnum):
    LIMIT_EXCEEDED = "limit_exceeded"


@dataclass(frozen=True, slots=True)
class ModelCheckpointPolicy:
    schema_version: Literal[1]
    active_per_session: int
    active_global: int
    inline_response_bytes: int
    frame_bytes: int
    reconcile_batch: int
    reconcile_seconds: float
    stream_soft_bytes: int
    stream_hard_bytes: int
    compaction_identities: int
    compaction_candidate_bytes: int
    compaction_seconds: float
    terminal_retention_days: int
    tombstone_retention_days: int

    def __post_init__(self) -> None:
        values = (
            self.active_per_session,
            self.active_global,
            self.inline_response_bytes,
            self.frame_bytes,
            self.reconcile_batch,
            self.stream_soft_bytes,
            self.stream_hard_bytes,
            self.compaction_identities,
            self.compaction_candidate_bytes,
            self.terminal_retention_days,
            self.tombstone_retention_days,
        )
        if self.schema_version != 1 or any(type(value) is not int or value < 1 for value in values):
            raise ValueError("Model checkpoint policy is invalid")
        if not 0 < self.reconcile_seconds <= 5 or not 0 < self.compaction_seconds <= 5:
            raise ValueError("Model checkpoint scan/compaction duration is invalid")
        hard_limits = {
            "active_per_session": 100,
            "active_global": 1_000,
            "inline_response_bytes": 64 * 1024,
            "frame_bytes": 2 * 1024 * 1024,
            "reconcile_batch": 200,
            "stream_soft_bytes": 64 * 1024 * 1024,
            "stream_hard_bytes": 256 * 1024 * 1024,
            "compaction_identities": 1_000,
            "compaction_candidate_bytes": 64 * 1024 * 1024,
            "terminal_retention_days": 90,
            "tombstone_retention_days": 365,
        }
        for name, limit in hard_limits.items():
            if getattr(self, name) > limit:
                raise ValueError(f"Model checkpoint policy {name} exceeds its approved hard limit")
        if self.active_per_session > self.active_global or self.stream_soft_bytes > self.stream_hard_bytes:
            raise ValueError("Model checkpoint policy bounds are inconsistent")


def require_narrower_attempt_budget(parent: AttemptBudget, candidate: AttemptBudget) -> AttemptBudget:
    """Reject any extension attempt to increase approved wire authority."""

    fields = (
        "max_wire_attempts",
        "max_attempts_per_endpoint",
        "max_endpoint_switches",
        "max_credential_rotations",
        "max_request_transforms",
        "total_deadline_seconds",
        "single_attempt_timeout_seconds",
        "max_backoff_seconds",
    )
    if any(getattr(candidate, field) > getattr(parent, field) for field in fields):
        raise ValueError("Model attempt budget may only be narrowed")
    return candidate


__all__ = [
    "ModelCheckpointAdmissionDisposition",
    "ModelCheckpointPolicy",
    "require_narrower_attempt_budget",
]
