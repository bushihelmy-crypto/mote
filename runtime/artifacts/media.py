"""Shared publication seam for immutable media returned by builtin tools."""
from __future__ import annotations

import hashlib

from mote.contracts.artifact import (
    ArtifactPublishRequest,
    ArtifactRef,
    ArtifactRepresentationInput,
    ArtifactRetention,
    ArtifactSensitivity,
)
from mote.contracts.ports.artifact.store import ReliableArtifactPublisher


async def publish_media_artifact(
    publisher: ReliableArtifactPublisher,
    *,
    content: bytes,
    representation: str,
    kind: str,
    mime_type: str,
    suggested_name: str = "",
    retention: ArtifactRetention = ArtifactRetention.SESSION,
    sensitivity: ArtifactSensitivity = ArtifactSensitivity.PRIVATE,
) -> ArtifactRef:
    """Publish content-addressed media and return its opaque representation ref."""
    identity = hashlib.sha256()
    for component in (
        representation.encode("utf-8"),
        kind.encode("utf-8"),
        mime_type.encode("utf-8"),
        suggested_name.encode("utf-8"),
        content,
    ):
        identity.update(len(component).to_bytes(8, "big"))
        identity.update(component)
    artifact_id = f"{kind}-{identity.hexdigest()}"
    request = ArtifactPublishRequest(
        artifact_id=artifact_id,
        expected_revision=0,
        retention=retention,
        sensitivity=sensitivity,
        representations=(
            ArtifactRepresentationInput(
                representation=representation,
                kind=kind,
                mime_type=mime_type,
                content=content,
                suggested_name=suggested_name,
            ),
        ),
    )
    revision = await publisher.publish(artifact_id, request)
    return revision.get(representation)


__all__ = ["publish_media_artifact"]
