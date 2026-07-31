"""Lossless local transfer records for moving Artifacts between scopes."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.artifact import ArtifactRevision


@dataclass(frozen=True, slots=True)
class ArtifactIdempotencyRecord:
    idempotency_key: str
    artifact_id: str
    revision: int
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ArtifactRevisionTransfer:
    revision: ArtifactRevision
    contents: tuple[bytes, ...]
    idempotency_records: tuple[ArtifactIdempotencyRecord, ...] = ()


__all__ = ["ArtifactIdempotencyRecord", "ArtifactRevisionTransfer"]
