"""Ports for immutable Artifact metadata and content storage."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.artifact import (
    ArtifactContentRef,
    ArtifactPublication,
    ArtifactPublicationIntent,
    ArtifactPublicationReconcileResult,
    ArtifactPublishRequest,
    ArtifactRef,
    ArtifactResolutionPolicy,
    ArtifactRetention,
    ArtifactRevision,
    ResolvedArtifact,
)


@runtime_checkable
class ArtifactBlobStore(Protocol):
    """Content-addressed byte storage below the logical Artifact index."""

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        ...

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        """Resolve only this store's CAS refs and verify their digest and size."""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    def scan_content_roots(self) -> tuple[ArtifactContentRef, ...]:
        """Return CAS refs protected by committed or recoverable metadata."""
        ...


@runtime_checkable
class ArtifactLookupIndex(Protocol):
    """Generic strong-key lookup into already committed Artifact revisions."""

    async def publish_lookup(self, lookup_key: str, artifact_id: str, revision: int) -> None:
        ...

    async def resolve_lookup(self, lookup_key: str) -> ArtifactRevision | None:
        ...

    async def publish(self, request: ArtifactPublishRequest) -> ArtifactRevision:
        ...

    async def get_revision(self, artifact_id: str, revision: int) -> ArtifactRevision:
        ...

    async def read(self, ref: ArtifactRef) -> bytes:
        ...

    async def promote(
        self,
        artifact_id: str,
        revision: int,
        retention: ArtifactRetention,
    ) -> ArtifactRevision:
        ...

    async def release(self, artifact_id: str, revision: int) -> bool:
        """Remove one logical revision and unroot its unshared CAS content."""
        ...

    def release_retentions(self, retentions: tuple[ArtifactRetention, ...]) -> int:
        """Release visible ownership rows in the requested lifecycle tiers."""
        ...


@runtime_checkable
class ArtifactResolver(Protocol):
    """Resolve an opaque ArtifactRef under an explicit caller policy."""

    async def resolve(
        self,
        ref: ArtifactRef,
        policy: ArtifactResolutionPolicy,
    ) -> ResolvedArtifact:
        ...


@runtime_checkable
class ArtifactPublicationOutbox(Protocol):
    """Crash-durable staging boundary for retryable Artifact publications."""

    async def stage(
        self,
        publication_id: str,
        request: ArtifactPublishRequest,
    ) -> ArtifactPublication:
        ...

    async def stage_intent(
        self,
        intent: ArtifactPublicationIntent,
    ) -> ArtifactPublication:
        """Bind trusted CAS refs to the outbox after validating every ref."""
        ...

    async def pending_ids(self, limit: int = 100) -> tuple[str, ...]:
        ...

    async def load(self, publication_id: str) -> ArtifactPublication:
        ...

    async def acknowledge(
        self,
        publication_id: str,
        revision: ArtifactRevision,
    ) -> None:
        ...

    async def record_failure(self, publication_id: str, error: str) -> None:
        ...

    async def dead_letter(self, publication_id: str, error: str) -> None:
        ...


@runtime_checkable
class ReliableArtifactPublisher(Protocol):
    """Stage-before-publish service with restart reconciliation."""

    async def publish(
        self,
        publication_id: str,
        request: ArtifactPublishRequest,
    ) -> ArtifactRevision:
        ...

    async def publish_intent(
        self,
        intent: ArtifactPublicationIntent,
    ) -> ArtifactRevision:
        ...

    async def reconcile_pending(
        self,
        limit: int = 100,
    ) -> ArtifactPublicationReconcileResult:
        ...


__all__ = [
    "ArtifactBlobStore",
    "ArtifactLookupIndex",
    "ArtifactPublicationOutbox",
    "ArtifactResolver",
    "ArtifactStore",
    "ReliableArtifactPublisher",
]
