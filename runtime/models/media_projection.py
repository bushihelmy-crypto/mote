"""Materialize durable tool media before command history projection."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable

from mote.contracts.artifact import ArtifactResolutionPolicy, ArtifactSensitivity
from mote.contracts.ports.artifact.store import ArtifactResolver
from mote.contracts.tool.result import ToolMedia
from mote.kernel.commands.contracts import ExecutedCommand

MediaMaterializer = Callable[[list[ExecutedCommand]], Awaitable[tuple[list[str], list[str]]]]

MODEL_MEDIA_ARTIFACT_POLICY = ArtifactResolutionPolicy(
    max_bytes=20 * 1024 * 1024,
    allowed_sensitivities=frozenset({ArtifactSensitivity.PUBLIC, ArtifactSensitivity.PRIVATE}),
)
_MODEL_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"})


def build_media_materializer(resolver: ArtifactResolver) -> MediaMaterializer:
    async def collect(
        executed: list[ExecutedCommand],
    ) -> tuple[list[str], list[str]]:
        async def materialize(media: ToolMedia) -> tuple[str, str] | None:
            kind = media.kind
            artifact = media.artifact
            if kind == "image" and artifact.mime_type not in _MODEL_IMAGE_MIME_TYPES:
                return None
            if kind == "pdf" and artifact.mime_type != "application/pdf":
                return None
            resolved = await resolver.resolve(artifact, MODEL_MEDIA_ARTIFACT_POLICY)
            return kind, base64.b64encode(resolved.content).decode("ascii")

        structured = [media for entry in executed for media in entry.media]
        resolved = await asyncio.gather(*(materialize(media) for media in structured))
        images = [payload for item in resolved if item is not None for kind, payload in (item,) if kind == "image"]
        pdfs = [payload for item in resolved if item is not None for kind, payload in (item,) if kind == "pdf"]
        return images, pdfs

    return collect


__all__ = ["MediaMaterializer", "build_media_materializer"]
