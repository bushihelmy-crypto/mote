"""Product-owned durable Artifact capabilities for inference generations."""

from __future__ import annotations

import hashlib
from pathlib import Path

from mote.contracts.artifact import (
    ArtifactPublishRequest,
    ArtifactRef,
    ArtifactRepresentationInput,
    ArtifactResolutionPolicy,
    ArtifactRetention,
    ArtifactSensitivity,
    ResolvedArtifact,
)
from mote.runtime.artifacts import ArtifactRepositoryLayout, ReliableArtifactPublisher, StoreArtifactResolver


class ProductInferenceArtifacts:
    """One persistent Artifact catalog shared by all local generations."""

    def __init__(self, state_root: Path) -> None:
        layout = ArtifactRepositoryLayout(state_root / "inference")
        ownership = layout.ownership(
            session_id="inference-runtime",
            project_root=state_root,
        )
        self._store = layout.open(ownership).store
        self._resolver = StoreArtifactResolver(self._store)
        self._publisher = ReliableArtifactPublisher(self._store, self._store)

    @property
    def store(self):
        return self._store

    async def resolve(self, ref: ArtifactRef) -> ResolvedArtifact:
        return await self._resolver.resolve(
            ref,
            ArtifactResolutionPolicy(
                max_bytes=ref.size,
                allowed_sensitivities=frozenset({ArtifactSensitivity.PUBLIC, ArtifactSensitivity.PRIVATE}),
            ),
        )

    async def publish(self, content: bytes, mime_type: str, suggested_name: str) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        identity = hashlib.sha256(
            b"mote-inference-artifact-v1\0"
            + digest.encode()
            + b"\0"
            + mime_type.encode()
            + b"\0"
            + suggested_name.encode()
        ).hexdigest()
        revision = await self._publisher.publish(
            f"inference-{identity}",
            ArtifactPublishRequest(
                idempotency_key=f"inference-{identity}",
                retention=ArtifactRetention.PROJECT,
                sensitivity=ArtifactSensitivity.PRIVATE,
                representations=(
                    ArtifactRepresentationInput(
                        representation="original",
                        kind="provider_file",
                        mime_type=mime_type,
                        content=content,
                        suggested_name=suggested_name,
                    ),
                ),
            ),
        )
        return revision.get("original")


__all__ = ["ProductInferenceArtifacts"]
