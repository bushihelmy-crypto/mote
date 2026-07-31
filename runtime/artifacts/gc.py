"""Reachability-driven garbage collection for the workspace Artifact CAS."""

from __future__ import annotations

import time

from mote.runtime.artifacts.ports import ArtifactMetadataSource, ArtifactPinSource, ArtifactRootSource
from mote.runtime.artifacts.repository import ArtifactRepository
from mote.runtime.artifacts.store import DurableArtifactStore


class ArtifactGarbageCollector:
    """Reclaim CAS objects not referenced by the durable Artifact index."""

    def __init__(
        self,
        store: DurableArtifactStore,
        repository: ArtifactRepository,
        *,
        root_sources: tuple[ArtifactRootSource, ...] = (),
        pin_sources: tuple[ArtifactPinSource, ...] = (),
        metadata_sources: tuple[ArtifactMetadataSource, ...] = (),
        minimum_age_ns: int = 0,
    ) -> None:
        self._store = store
        self._repository = repository
        self._root_sources = root_sources
        self._pin_sources = pin_sources
        self._metadata_sources = metadata_sources
        self._minimum_age_ns = minimum_age_ns

    def collect(self) -> int:
        reachable = {ref.identity.digest for ref in self._store.scan_content_roots()}
        for source in self._root_sources:
            reachable.update(ref.identity.digest for ref in source.artifact_roots())
        pinned = set()
        pin_leases = []
        try:
            for source in self._pin_sources:
                lease = source.freeze_artifact_pins()
                pinned.update(ref.identity.digest for ref in lease.__enter__())
                pin_leases.append(lease)
            reachable.update(pinned)
            canonical = tuple(ref for ref in self._repository.scan() if ref.identity.digest in reachable)
            for source in self._metadata_sources:
                source.prune_artifact_metadata(canonical)
            cutoff = time.time_ns() - self._minimum_age_ns
            reclaimed = 0
            for ref in self._repository.scan():
                if ref.identity.digest in reachable or self._repository.modified_time_ns(ref) > cutoff:
                    continue
                reclaimed += self._repository.reclaim(ref)
            return reclaimed
        finally:
            for lease in reversed(pin_leases):
                lease.__exit__(None, None, None)


__all__ = ["ArtifactGarbageCollector"]
