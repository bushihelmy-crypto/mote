"""Fenced staged execution of an authorized Session deletion command."""

from __future__ import annotations

import shutil
from pathlib import Path

from mote.contracts.artifact import ArtifactOwnerKind
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.contracts.session import (
    SessionDeletionClaim,
    SessionDeletionCommand,
    SessionDeletionReceipt,
    SessionDeletionState,
)
from mote.runtime.artifacts.store import DurableArtifactStore
from mote.runtime.session.lifecycle import SessionLifecycleStore


class SessionDeletionExecutor:
    """Runtime mechanism; Product supplies command authority and the current lease."""

    def __init__(
        self,
        lifecycle: SessionLifecycleStore,
        artifacts: DurableArtifactStore,
        sessions_root: Path,
        *,
        lease_coordinator: LeaseCoordinator,
        lease: LeaseEpoch,
    ) -> None:
        self._lifecycle = lifecycle
        self._artifacts = artifacts
        self._sessions_root = Path(sessions_root).resolve()
        self._leases = lease_coordinator
        self._lease = lease

    def execute(self, command: SessionDeletionCommand) -> SessionDeletionReceipt:
        self._assert_owner()
        claim = self._lifecycle.claim_deletion(
            command, owner_id=self._lease.owner_id, fencing_token=self._lease.fencing_token
        )
        try:
            claim = self._advance(claim, SessionDeletionState.REFERENCES_RELEASING)
            edges = self._artifacts.ownership_edges(
                owner_kind=ArtifactOwnerKind.SESSION,
                owner_id=str(claim.session_id),
            )
            edge_generations = {edge.generation for edge in edges}
            if len(edge_generations) > 1:
                raise RuntimeError("Session Artifact edge snapshot has mixed generations")
            edge_generation = next(iter(edge_generations), 1)
            self._artifacts.release_ownership_edges(
                owner_kind=ArtifactOwnerKind.SESSION,
                owner_id=str(claim.session_id),
                expected_generation=edge_generation,
                release_generation=edge_generation + 1,
            )
            claim = self._advance(claim, SessionDeletionState.METADATA_TOMBSTONED)
            claim = self._advance(claim, SessionDeletionState.BLOBS_RECLAIMING)
            claim = self._advance(claim, SessionDeletionState.DIRECTORY_RETIRING)
            self._retire_directory(claim)
            return self._lifecycle.advance_deletion(claim, SessionDeletionState.SETTLED)
        except BaseException as exc:
            self._lifecycle.advance_deletion(claim, SessionDeletionState.IN_DOUBT, detail=type(exc).__name__)
            raise

    def _advance(self, claim: SessionDeletionClaim, state: SessionDeletionState) -> SessionDeletionClaim:
        self._assert_owner()
        receipt = self._lifecycle.advance_deletion(claim, state)
        return SessionDeletionClaim(
            claim.command_id,
            claim.session_id,
            claim.lifecycle_generation,
            receipt.revision,
            claim.owner_id,
            claim.fencing_token,
        )

    def _retire_directory(self, claim: SessionDeletionClaim) -> None:
        self._assert_owner()
        candidate = self._sessions_root / str(claim.session_id)
        if candidate.parent.resolve() != self._sessions_root or candidate.is_symlink():
            raise RuntimeError("Session deletion target escapes the canonical sessions root")
        if candidate.exists():
            shutil.rmtree(candidate)
        self._assert_owner()

    def _assert_owner(self) -> None:
        self._leases.assert_current(self._lease.subject, self._lease.fencing_token)


__all__ = ["SessionDeletionExecutor"]
