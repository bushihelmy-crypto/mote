"""Artifact blob adapter over the workspace Artifact repository."""

from __future__ import annotations

from mote.contracts.artifact import ArtifactContentRef
from mote.runtime.artifacts.repository import ContentAddressedArtifactStore


class ContentAddressedArtifactBlobStore:
    """Expose the verified content store through the generic Artifact blob port."""

    def __init__(self, repository: ContentAddressedArtifactStore) -> None:
        if type(repository) is not ContentAddressedArtifactStore:
            raise TypeError("artifact blob adapter requires a ContentAddressedArtifactStore")
        self._repository = repository

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        if type(content) is not bytes:
            raise TypeError("artifact blob content must be bytes")
        return self._repository.put_bytes(content)

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        return self._repository.read_bytes(ref)


__all__ = ["ContentAddressedArtifactBlobStore"]
