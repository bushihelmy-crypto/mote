"""Artifact capability used only for oversized canonical Model responses."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.artifact import ArtifactRef


class ModelResponseArtifactPublisher(Protocol):
    async def __call__(self, content: bytes, mime_type: str, suggested_name: str) -> ArtifactRef: ...


__all__ = ["ModelResponseArtifactPublisher"]
