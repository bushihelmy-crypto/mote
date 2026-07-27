"""Two-pass, generation-fenced garbage collection for durable artifacts."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from mote.contracts.fileops.errors import SnapshotDurabilityError
from mote.contracts.fileops.models import BlobRef
from mote.runtime.fileops.artifact_lifecycle import ArtifactObject, ArtifactObjectState
from mote.runtime.fileops.artifact_reachability import ArtifactReachability
from mote.runtime.fileops.artifact_repository import ArtifactReclaimResult, ArtifactReclaimStatus, ArtifactRepository
from mote.runtime.fileops.cursor_registry import ArtifactPinSnapshot


class ArtifactGarbageCollectionConflictError(SnapshotDurabilityError):
    """A root or pin authority changed while a GC pass was scanning it."""


class ArtifactReachabilitySource(Protocol):
    def scan(self) -> ArtifactReachability:
        ...


class ArtifactPinSource(Protocol):
    def freeze_pins(self) -> AbstractContextManager[ArtifactPinSnapshot]:
        ...


@dataclass(frozen=True, slots=True)
class ArtifactQuarantinePass:
    source_generation: int
    pin_epoch: int
    pin_revision: int
    protected_artifacts: tuple[BlobRef, ...]
    quarantined_objects: tuple[ArtifactObject, ...]


@dataclass(frozen=True, slots=True)
class ArtifactDeletionPass:
    source_generation: int
    pin_epoch: int
    pin_revision: int
    protected_artifacts: tuple[BlobRef, ...]
    restored_objects: tuple[ArtifactObject, ...]
    deletion_candidates: tuple[ArtifactObject, ...]


@dataclass(frozen=True, slots=True)
class ArtifactReclamationPass:
    candidates: tuple[ArtifactObject, ...]
    results: tuple[ArtifactReclaimResult, ...]


@dataclass(frozen=True, slots=True)
class ArtifactGarbageCollectionCycle:
    quarantine: ArtifactQuarantinePass
    deletion: ArtifactDeletionPass
    reclamation: ArtifactReclamationPass


class ArtifactGarbageCollector:
    """Coordinates two independent root scans around catalog state transitions."""

    def __init__(
        self,
        *,
        repository: ArtifactRepository,
        reachability: ArtifactReachabilitySource,
        pins: ArtifactPinSource,
        minimum_quarantine_age_ns: int,
    ) -> None:
        if type(repository) is not ArtifactRepository:
            raise TypeError("artifact GC requires an artifact repository")
        if type(minimum_quarantine_age_ns) is not int or minimum_quarantine_age_ns < 0:
            raise ValueError("artifact GC quarantine age must be non-negative")
        self.repository = repository
        self.catalog = repository.catalog
        self.reachability = reachability
        self.pins = pins
        self.minimum_quarantine_age_ns = minimum_quarantine_age_ns

    def quarantine(
        self,
        *,
        limit: int,
        now_ns: int | None = None,
    ) -> ArtifactQuarantinePass:
        if type(limit) is not int or limit <= 0:
            raise ValueError("artifact quarantine limit must be positive")
        catalog_snapshot = self.catalog.gc_snapshot()
        reachable = self.reachability.scan()
        with self.pins.freeze_pins() as pin_snapshot:
            protected = self._canonical_refs((*reachable.artifacts, *pin_snapshot.artifacts))
            self._validate_protected(catalog_snapshot.objects, protected)
            protected_digests = {artifact.digest for artifact in protected}
            candidates = tuple(
                item.artifact
                for item in catalog_snapshot.objects
                if item.state == ArtifactObjectState.LIVE and item.artifact.digest not in protected_digests
            )[:limit]
            quarantined = self.catalog.quarantine_unreachable(
                candidates,
                expected_generation=catalog_snapshot.generation,
                now_ns=now_ns,
            )
        return ArtifactQuarantinePass(
            source_generation=catalog_snapshot.generation,
            pin_epoch=pin_snapshot.epoch,
            pin_revision=pin_snapshot.revision,
            protected_artifacts=protected,
            quarantined_objects=quarantined,
        )

    def prepare_deletions(
        self,
        *,
        limit: int,
        minimum_age_ns: int | None = None,
        now_ns: int | None = None,
    ) -> ArtifactDeletionPass:
        if type(limit) is not int or limit <= 0:
            raise ValueError("artifact deletion preparation limit must be positive")
        if minimum_age_ns is None:
            minimum_age_ns = self.minimum_quarantine_age_ns
        if type(minimum_age_ns) is not int or minimum_age_ns < 0:
            raise ValueError("artifact deletion preparation age must be non-negative")
        catalog_snapshot = self.catalog.gc_snapshot()
        reachable = self.reachability.scan()
        with self.pins.freeze_pins() as pin_snapshot:
            protected = self._canonical_refs((*reachable.artifacts, *pin_snapshot.artifacts))
            self._validate_protected(catalog_snapshot.objects, protected)
            reconciliation = self.catalog.reconcile_quarantine(
                protected,
                expected_generation=catalog_snapshot.generation,
                minimum_age_ns=minimum_age_ns,
                maximum_deletions=limit,
                now_ns=now_ns,
            )
        return ArtifactDeletionPass(
            source_generation=catalog_snapshot.generation,
            pin_epoch=pin_snapshot.epoch,
            pin_revision=pin_snapshot.revision,
            protected_artifacts=protected,
            restored_objects=reconciliation.restored_objects,
            deletion_candidates=reconciliation.deletion_candidates,
        )

    def reclaim_deleting(self, *, limit: int) -> ArtifactReclamationPass:
        candidates = self.catalog.deletion_candidates(limit=limit)
        results = tuple(self.repository.reclaim(candidate) for candidate in candidates)
        return ArtifactReclamationPass(
            candidates=candidates,
            results=results,
        )

    def run_cycle(
        self,
        *,
        limit: int,
        expedited: bool = False,
        now_ns: int | None = None,
    ) -> ArtifactGarbageCollectionCycle:
        if type(limit) is not int or limit <= 0:
            raise ValueError("artifact garbage collection limit must be positive")
        if type(expedited) is not bool:
            raise TypeError("artifact garbage collection expedited mode must be boolean")
        try:
            quarantine = self.quarantine(limit=limit, now_ns=now_ns)
            deletion = self.prepare_deletions(
                limit=limit,
                minimum_age_ns=0 if expedited else None,
                now_ns=now_ns,
            )
            reclamation = self.reclaim_deleting(limit=limit)
            cycle = ArtifactGarbageCollectionCycle(
                quarantine=quarantine,
                deletion=deletion,
                reclamation=reclamation,
            )
            reclaimed = tuple(
                result.candidate.artifact
                for result in reclamation.results
                if result.status == ArtifactReclaimStatus.RECLAIMED
            )
            self.catalog.record_garbage_collection_success(
                quarantined_objects=len(quarantine.quarantined_objects),
                restored_objects=len(deletion.restored_objects),
                deletion_candidates=len(deletion.deletion_candidates),
                reclaimed_objects=len(reclaimed),
                reclaimed_bytes=sum(artifact.size for artifact in reclaimed),
                now_ns=now_ns,
            )
            return cycle
        except Exception as exc:
            failure = f"{type(exc).__name__}: {ascii(str(exc))}"
            self.catalog.record_garbage_collection_failure(
                failure[:4_096],
                now_ns=now_ns,
            )
            raise

    @staticmethod
    def _canonical_refs(artifacts: tuple[BlobRef, ...]) -> tuple[BlobRef, ...]:
        canonical: dict[str, BlobRef] = {}
        for artifact in artifacts:
            if type(artifact) is not BlobRef:
                raise TypeError("artifact GC protection contains an invalid reference")
            prior = canonical.setdefault(artifact.digest, artifact)
            if prior != artifact:
                raise ArtifactGarbageCollectionConflictError(
                    "artifact GC protection has conflicting sizes",
                    digest=artifact.digest,
                )
        return tuple(canonical[digest] for digest in sorted(canonical))

    @staticmethod
    def _validate_protected(
        objects: tuple[ArtifactObject, ...],
        protected: tuple[BlobRef, ...],
    ) -> None:
        catalog = {item.artifact.digest: item for item in objects}
        for artifact in protected:
            current = catalog.get(artifact.digest)
            if (
                current is None
                or current.artifact != artifact
                or current.state not in (ArtifactObjectState.LIVE, ArtifactObjectState.QUARANTINED)
            ):
                raise ArtifactGarbageCollectionConflictError(
                    "protected artifact is not an exact recoverable catalog object",
                    digest=artifact.digest,
                )


__all__ = [
    "ArtifactDeletionPass",
    "ArtifactGarbageCollectionCycle",
    "ArtifactReclamationPass",
    "ArtifactGarbageCollectionConflictError",
    "ArtifactGarbageCollector",
    "ArtifactPinSource",
    "ArtifactQuarantinePass",
    "ArtifactReachabilitySource",
]
