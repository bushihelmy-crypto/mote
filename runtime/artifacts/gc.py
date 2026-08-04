"""Reachability-driven garbage collection for the workspace Artifact CAS."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from mote.contracts.artifact import (
    ArtifactContentRef,
    ArtifactDeletionClaim,
    ArtifactDeletionCommand,
    ArtifactDeletionState,
)
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.runtime.artifacts.ports import ArtifactMetadataSource, ArtifactPinSource, ArtifactRootSource
from mote.runtime.artifacts.repository import ContentAddressedArtifactStore
from mote.runtime.artifacts.store import DurableArtifactStore


class ArtifactGarbageCollector:
    """Reclaim CAS objects not referenced by the durable Artifact index."""

    def __init__(
        self,
        store: DurableArtifactStore,
        repository: ContentAddressedArtifactStore,
        *,
        root_sources: tuple[tuple[str, ArtifactRootSource], ...] = (),
        pin_sources: tuple[tuple[str, ArtifactPinSource], ...] = (),
        metadata_sources: tuple[ArtifactMetadataSource, ...] = (),
        lease_coordinator: LeaseCoordinator,
        lease: LeaseEpoch,
        lease_ttl_seconds: float = 30.0,
        scan_limit: int = 256,
    ) -> None:
        self._store = store
        self._repository = repository
        self._root_sources = root_sources
        self._pin_sources = pin_sources
        self._metadata_sources = metadata_sources
        self._lease_coordinator = lease_coordinator
        self._lease = lease
        self._lease_ttl_seconds = lease_ttl_seconds
        if type(scan_limit) is not int or scan_limit < 1 or scan_limit > 4096:
            raise ValueError("Artifact GC scan limit is invalid")
        self._scan_limit = scan_limit

    def collect(self) -> int:
        self._lease = self._lease_coordinator.renew(self._lease, self._lease_ttl_seconds)
        self._lease_coordinator.assert_current(self._lease.subject, self._lease.fencing_token)
        producer_roots: dict[str, set[str]] = {
            "artifact-store": {str(ref.identity.digest) for ref in self._store.scan_content_roots()}
        }
        for producer_id, source in self._root_sources:
            producer_roots[producer_id] = {str(ref.identity.digest) for ref in source.artifact_roots()}
        pinned = set()
        pin_leases = []
        try:
            for producer_id, source in self._pin_sources:
                lease = source.freeze_artifact_pins()
                source_pins = {str(ref.identity.digest) for ref in lease.__enter__()}
                pinned.update(source_pins)
                producer_roots[producer_id] = source_pins
                pin_leases.append(lease)
            reachable = set().union(*producer_roots.values())
            closure_generation = self._store.publish_gc_closure(
                producer_roots=tuple(
                    (producer_id, tuple(sorted(digests))) for producer_id, digests in sorted(producer_roots.items())
                )
            )
            canonical = tuple(ref for ref in self._repository.scan() if ref.identity.digest in reachable)
            for source in self._metadata_sources:
                source.prune_artifact_metadata(canonical)
            reclaimed = 0
            repository_snapshot = self._repository.scan()
            by_digest = {str(ref.identity.digest): ref for ref in repository_snapshot}
            for command_id in self._store.scan_in_doubt_deletions(limit=self._scan_limit):
                claim = self._store.resume_in_doubt_deletion(
                    command_id, owner_id=self._lease.owner_id, fencing_token=self._lease.fencing_token
                )
                reclaimed += self._settle_claim(claim, by_digest.get(claim.content_digest))
            cursor = self._store.gc_cursor(closure_generation=closure_generation)
            page = tuple(ref for ref in repository_snapshot if str(ref.identity.digest) > cursor)[: self._scan_limit]
            if not page and repository_snapshot:
                page = repository_snapshot[: self._scan_limit]
            for ref in page:
                if ref.identity.digest in reachable:
                    self._store.advance_gc_cursor(
                        closure_generation=closure_generation, content_digest=str(ref.identity.digest)
                    )
                    continue
                command = ArtifactDeletionCommand(
                    command_id=f"gc:{uuid4().hex}",
                    content_digest=str(ref.identity.digest),
                    requested_by=self._lease.owner_id,
                    requested_at=datetime.now(timezone.utc),
                )
                claim = self._store.claim_unreachable_content(
                    command, owner_id=self._lease.owner_id, fencing_token=self._lease.fencing_token
                )
                if claim is None or not self._store.validate_deletion_claim(claim):
                    self._store.advance_gc_cursor(
                        closure_generation=closure_generation, content_digest=str(ref.identity.digest)
                    )
                    continue
                reclaimed += self._settle_claim(claim, ref)
                self._store.advance_gc_cursor(
                    closure_generation=closure_generation, content_digest=str(ref.identity.digest)
                )
            return reclaimed
        finally:
            for lease in reversed(pin_leases):
                lease.__exit__(None, None, None)

    def _advance(self, claim: ArtifactDeletionClaim, state: ArtifactDeletionState) -> ArtifactDeletionClaim:
        self._lease_coordinator.assert_current(self._lease.subject, self._lease.fencing_token)
        receipt = self._store.advance_deletion(claim, state)
        return ArtifactDeletionClaim(
            claim.command_id,
            claim.content_digest,
            claim.closure_generation,
            receipt.revision,
            claim.owner_id,
            claim.fencing_token,
        )

    def _settle_claim(self, claim: ArtifactDeletionClaim, ref: ArtifactContentRef | None) -> int:
        try:
            claim = self._advance(claim, ArtifactDeletionState.REFERENCES_RELEASING)
            claim = self._advance(claim, ArtifactDeletionState.METADATA_TOMBSTONED)
            claim = self._advance(claim, ArtifactDeletionState.BLOBS_RECLAIMING)
            self._lease_coordinator.assert_current(self._lease.subject, self._lease.fencing_token)
            reclaimed = 0 if ref is None else int(self._repository.reclaim(ref))
            claim = self._advance(claim, ArtifactDeletionState.DIRECTORY_RETIRING)
            self._advance(claim, ArtifactDeletionState.SETTLED)
            return reclaimed
        except BaseException as exc:
            self._store.advance_deletion(claim, ArtifactDeletionState.IN_DOUBT, detail=type(exc).__name__)
            raise


__all__ = ["ArtifactGarbageCollector"]
