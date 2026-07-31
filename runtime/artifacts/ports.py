"""Runtime-internal seams between Artifact storage and domain root providers."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, Sequence

from mote.contracts.artifact import ArtifactContentRef


class ArtifactRootSource(Protocol):
    def artifact_roots(self) -> Sequence[ArtifactContentRef]:
        ...


class ArtifactPinSource(Protocol):
    def freeze_artifact_pins(
        self,
    ) -> AbstractContextManager[Sequence[ArtifactContentRef]]:
        ...


class ArtifactReservationJournal(Protocol):
    def recover_artifact_reservations(self) -> None:
        ...


class ArtifactMetadataSource(Protocol):
    def prune_artifact_metadata(self, reachable: Sequence[ArtifactContentRef]) -> None:
        ...


__all__ = [
    "ArtifactMetadataSource",
    "ArtifactPinSource",
    "ArtifactReservationJournal",
    "ArtifactRootSource",
]
