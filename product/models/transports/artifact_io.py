"""Narrow Artifact I/O capabilities used by provider transports."""

from collections.abc import Awaitable, Callable

from mote.contracts.artifact import ArtifactRef, ResolvedArtifact

ArtifactResolver = Callable[[ArtifactRef], Awaitable[ResolvedArtifact]]
ArtifactPublisher = Callable[[bytes, str, str], Awaitable[ArtifactRef]]

__all__ = ["ArtifactPublisher", "ArtifactResolver"]
