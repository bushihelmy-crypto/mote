"""Reachability-driven garbage collection for the workspace Artifact store."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from mote.contracts.fileops.models import BlobRef
from mote.runtime.fileops.artifact_gc import ArtifactGarbageCollector
from mote.runtime.fileops.artifact_reachability import ArtifactReachability, ArtifactRoot, ArtifactRootKind
from mote.runtime.fileops.artifact_repository import ArtifactRepository
from mote.runtime.fileops.cursor_registry import ArtifactPinSnapshot

from .store import DurableArtifactStore

_COLLECTION_BATCH_SIZE = 1_024


class _ArtifactStoreReachability:
    def __init__(self, store: DurableArtifactStore) -> None:
        self._store = store

    def scan(self) -> ArtifactReachability:
        refs = tuple(BlobRef(digest=item.digest, size=item.size) for item in self._store.scan_content_roots())
        roots = tuple(ArtifactRoot(ref, ArtifactRootKind.LEAF, "artifact-index") for ref in refs)
        return ArtifactReachability(roots=roots, artifacts=refs)


class _NoArtifactPins:
    @contextmanager
    def freeze_pins(self) -> Iterator[ArtifactPinSnapshot]:
        yield ArtifactPinSnapshot(epoch=0, revision=0, artifacts=())


class ArtifactStoreGarbageCollector:
    """Drain unreachable objects from the catalog's co-located CAS."""

    def __init__(
        self,
        store: DurableArtifactStore,
        repository: ArtifactRepository,
    ) -> None:
        self._collector = ArtifactGarbageCollector(
            repository=repository,
            reachability=_ArtifactStoreReachability(store),
            pins=_NoArtifactPins(),
            minimum_quarantine_age_ns=0,
        )

    def collect(self) -> int:
        reclaimed = 0
        while True:
            cycle = self._collector.run_cycle(
                limit=_COLLECTION_BATCH_SIZE,
                expedited=True,
            )
            reclaimed += len(cycle.reclamation.results)
            saturated = any(
                len(items) >= _COLLECTION_BATCH_SIZE
                for items in (
                    cycle.quarantine.quarantined_objects,
                    cycle.deletion.deletion_candidates,
                    cycle.reclamation.candidates,
                )
            )
            if not saturated:
                return reclaimed


__all__ = ["ArtifactStoreGarbageCollector"]
