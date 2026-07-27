"""Artifact blob adapter over the workspace Artifact repository."""
from __future__ import annotations

from mote.contracts.artifacts import ArtifactContentRef
from mote.contracts.fileops.models import BlobRef
from mote.runtime.fileops.artifact_repository import ArtifactRepository

_RESERVATION_TTL_SECONDS = 300.0


class ArtifactRepositoryBlobStore:
    """Expose an ArtifactRepository through the generic Artifact blob port."""

    def __init__(self, repository: ArtifactRepository) -> None:
        if type(repository) is not ArtifactRepository:
            raise TypeError("artifact blob adapter requires an ArtifactRepository")
        self._repository = repository

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        if type(content) is not bytes:
            raise TypeError("artifact blob content must be bytes")
        scope = self._repository.write_scope(
            owner="artifact-publication",
            maximum_bytes=len(content),
            ttl_seconds=_RESERVATION_TTL_SECONDS,
        )
        with scope:
            ref = scope.put_bytes(content)
            scope.complete(durability_root=self._repository.root)
        return ArtifactContentRef(
            content_ref=f"sha256:{ref.digest}",
            digest=ref.digest,
            size=ref.size,
        )

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        if ref.content_ref != f"sha256:{ref.digest}":
            raise ValueError("artifact content reference is invalid for repository CAS")
        return self._repository.read_bytes(BlobRef(digest=ref.digest, size=ref.size))


__all__ = ["ArtifactRepositoryBlobStore"]
