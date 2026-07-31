"""Small immutable ArtifactRef fixtures for tests that do not need a real CAS."""
from __future__ import annotations

import hashlib

from mote.contracts.artifact import ArtifactRef, ResolvedArtifact
from mote.runtime.tools.tool_result import ToolMedia

_CONTENT_BY_DIGEST: dict[str, bytes] = {}


def artifact_ref(
    content: bytes | str = b"test-media",
    *,
    kind: str = "test-media",
    representation: str = "bin",
    mime_type: str = "application/octet-stream",
) -> ArtifactRef:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    digest = hashlib.sha256(raw).hexdigest()
    _CONTENT_BY_DIGEST[digest] = raw
    return ArtifactRef(
        artifact_id=f"{kind}-{digest}",
        revision=1,
        representation=representation,
        kind=kind,
        mime_type=mime_type,
        content_ref=f"sha256:{digest}",
        digest=digest,
        size=len(raw),
    )


def artifact_media(
    media_kind: str = "image",
    content: bytes | str = b"test-media",
    *,
    ref: str = "",
) -> ToolMedia:
    mime_type = "application/pdf" if media_kind == "pdf" else "image/png"
    representation = "pdf" if media_kind == "pdf" else "png"
    return ToolMedia(
        artifact=artifact_ref(
            content,
            kind=f"test-{media_kind}",
            representation=representation,
            mime_type=mime_type,
        ),
        kind=media_kind,
        ref=ref,
        mime=mime_type,
    )


class ArtifactTestResolver:
    async def resolve(self, ref: ArtifactRef, policy) -> ResolvedArtifact:
        return ResolvedArtifact(ref=ref, content=_CONTENT_BY_DIGEST[ref.digest])


__all__ = ["ArtifactTestResolver", "artifact_media", "artifact_ref"]
