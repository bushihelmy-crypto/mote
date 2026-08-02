"""Stable contracts for durable Agent lineage and spawn reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SpawnLifecycle(StrEnum):
    REQUESTED = "requested"
    ADMITTED = "admitted"
    LINEAGE_COMMITTED = "lineage_committed"
    PLACEMENT_PENDING = "placement_pending"
    INCARNATION_STARTED = "incarnation_started"
    ACTIVE = "active"
    REJECTED = "rejected"
    ABORTED = "aborted"
    TERMINAL = "terminal"


class SpawnAdvanceDisposition(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    STALE_REVISION = "stale_revision"
    STALE_FENCE = "stale_fence"
    NOT_FOUND = "not_found"


class LineageAuthorizationDisposition(StrEnum):
    AUTHORIZED = "authorized"
    NOT_FOUND = "not_found"
    NOT_ACTIVE = "not_active"
    INCARNATION_MISMATCH = "incarnation_mismatch"
    STALE_FENCE = "stale_fence"


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    request_id: str
    root_agent_id: str
    parent_agent_id: str | None
    agent_path: str
    nickname: str | None
    definition_id: str
    capacity_reservation_id: str
    budget_reservation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        required = (
            self.request_id,
            self.root_agent_id,
            self.agent_path,
            self.definition_id,
            self.capacity_reservation_id,
        )
        if any(type(value) is not str or not value for value in required):
            raise ValueError("spawn request identity is invalid")
        if self.parent_agent_id is not None and not self.parent_agent_id:
            raise ValueError("spawn parent identity is invalid")
        if self.nickname is not None and not self.nickname:
            raise ValueError("spawn nickname is invalid")
        if len(set(self.budget_reservation_ids)) != len(self.budget_reservation_ids):
            raise ValueError("spawn budget reservation identities are duplicated")
        if any(type(value) is not str or not value for value in self.budget_reservation_ids):
            raise ValueError("spawn budget reservation identity is invalid")


@dataclass(frozen=True, slots=True)
class LineageRecord:
    request: SpawnRequest
    logical_agent_id: str | None
    lifecycle: SpawnLifecycle
    revision: int
    path_revision: int
    nickname_revision: int | None
    incarnation_generation: int
    placement: str | None
    owner_fencing_token: int
    cancellation_epoch: int = 0
    tombstoned: bool = False


@dataclass(frozen=True, slots=True)
class SpawnAdvanceReceipt:
    request_id: str
    disposition: SpawnAdvanceDisposition
    record: LineageRecord | None


@dataclass(frozen=True, slots=True)
class SubtreeLineageSnapshot:
    root_agent_id: str
    subtree_agent_id: str
    revision: int
    agent_ids: tuple[str, ...]
    cancellation_epoch: int = 0
    workflow_create_admission_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LineageAuthorizationReceipt:
    agent_id: str
    disposition: LineageAuthorizationDisposition
    lineage_revision: int


__all__ = [
    "LineageRecord",
    "LineageAuthorizationDisposition",
    "LineageAuthorizationReceipt",
    "SpawnAdvanceDisposition",
    "SpawnAdvanceReceipt",
    "SpawnLifecycle",
    "SpawnRequest",
    "SubtreeLineageSnapshot",
]
