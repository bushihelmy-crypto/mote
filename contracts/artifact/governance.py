"""Closed ownership, retention, and deletion contracts for Artifact content."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


def _identity(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{field} is invalid")


def _positive(value: int, field: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field} is invalid")


def _instant(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


class ArtifactOwnerKind(StrEnum):
    SESSION = "session"
    WORKFLOW = "workflow"
    BACKGROUND_TASK = "background_task"
    TOOL_EFFECT = "tool_effect"
    MODEL_CALL = "model_call"
    SERVICE_CALL = "service_call"
    AGENT_DELIVERY = "agent_delivery"
    FILE_OPERATION = "file_operation"
    PUBLICATION = "publication"
    PROJECT = "project"


class ArtifactHoldKind(StrEnum):
    ACTIVE_OPERATION = "active_operation"
    DELIVERY_SETTLEMENT = "delivery_settlement"
    EFFECT_SETTLEMENT = "effect_settlement"
    LEGAL = "legal"
    RETENTION = "retention"


class ArtifactDeletionState(StrEnum):
    REQUESTED = "requested"
    CLAIMED = "claimed"
    REFERENCES_RELEASING = "references_releasing"
    METADATA_TOMBSTONED = "metadata_tombstoned"
    BLOBS_RECLAIMING = "blobs_reclaiming"
    DIRECTORY_RETIRING = "directory_retiring"
    SETTLED = "settled"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class ArtifactOwnershipEdge:
    owner_kind: ArtifactOwnerKind
    owner_id: str
    content_digest: str
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.owner_kind, ArtifactOwnerKind):
            raise TypeError("Artifact edge owner kind is invalid")
        _identity(self.owner_id, "Artifact edge owner identity")
        _identity(self.content_digest, "Artifact edge content identity")
        _positive(self.generation, "Artifact edge generation")


@dataclass(frozen=True, slots=True)
class ArtifactHold:
    hold_id: str
    kind: ArtifactHoldKind
    content_digest: str
    owner_id: str
    generation: int
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _identity(self.hold_id, "Artifact hold identity")
        if not isinstance(self.kind, ArtifactHoldKind):
            raise TypeError("Artifact hold kind is invalid")
        _identity(self.content_digest, "Artifact hold content identity")
        _identity(self.owner_id, "Artifact hold owner identity")
        _positive(self.generation, "Artifact hold generation")
        if self.expires_at is not None:
            _instant(self.expires_at, "Artifact hold expiry")


@dataclass(frozen=True, slots=True)
class ArtifactCompletenessManifest:
    generation: int
    producer_ids: tuple[str, ...]
    committed_at: datetime

    def __post_init__(self) -> None:
        _positive(self.generation, "Artifact completeness generation")
        if not self.producer_ids or tuple(sorted(set(self.producer_ids))) != self.producer_ids:
            raise ValueError("Artifact completeness producers must be non-empty, unique, and sorted")
        for producer_id in self.producer_ids:
            _identity(producer_id, "Artifact completeness producer identity")
        _instant(self.committed_at, "Artifact completeness commit time")


@dataclass(frozen=True, slots=True)
class ArtifactDeletionCommand:
    command_id: str
    content_digest: str
    requested_by: str
    requested_at: datetime

    def __post_init__(self) -> None:
        _identity(self.command_id, "Artifact deletion command identity")
        _identity(self.content_digest, "Artifact deletion content identity")
        _identity(self.requested_by, "Artifact deletion authority")
        _instant(self.requested_at, "Artifact deletion request time")


@dataclass(frozen=True, slots=True)
class ArtifactDeletionClaim:
    command_id: str
    content_digest: str
    closure_generation: int
    claim_revision: int
    owner_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        _identity(self.command_id, "Artifact deletion command identity")
        _identity(self.content_digest, "Artifact deletion content identity")
        _positive(self.closure_generation, "Artifact deletion closure generation")
        _positive(self.claim_revision, "Artifact deletion claim revision")
        _identity(self.owner_id, "Artifact deletion owner identity")
        _positive(self.fencing_token, "Artifact deletion fencing token")


@dataclass(frozen=True, slots=True)
class ArtifactDeletionReceipt:
    command_id: str
    content_digest: str
    state: ArtifactDeletionState
    revision: int
    updated_at: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        _identity(self.command_id, "Artifact deletion command identity")
        _identity(self.content_digest, "Artifact deletion content identity")
        if not isinstance(self.state, ArtifactDeletionState):
            raise TypeError("Artifact deletion state is invalid")
        _positive(self.revision, "Artifact deletion revision")
        _instant(self.updated_at, "Artifact deletion update time")
        if type(self.detail) is not str or len(self.detail) > 4096:
            raise ValueError("Artifact deletion detail is invalid")


ARTIFACT_DELETION_TOMBSTONE_RETENTION_SECONDS = 365 * 24 * 60 * 60
EPHEMERAL_RETENTION_SECONDS = 24 * 60 * 60
SESSION_RETENTION_SECONDS = 30 * 24 * 60 * 60


__all__ = [
    "ARTIFACT_DELETION_TOMBSTONE_RETENTION_SECONDS",
    "EPHEMERAL_RETENTION_SECONDS",
    "SESSION_RETENTION_SECONDS",
    "ArtifactCompletenessManifest",
    "ArtifactDeletionClaim",
    "ArtifactDeletionCommand",
    "ArtifactDeletionReceipt",
    "ArtifactDeletionState",
    "ArtifactHold",
    "ArtifactHoldKind",
    "ArtifactOwnerKind",
    "ArtifactOwnershipEdge",
]
