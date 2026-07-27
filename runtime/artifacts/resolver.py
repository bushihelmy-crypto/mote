"""Policy-bound, integrity-verifying Artifact resolution."""
from __future__ import annotations

from mote.contracts.artifacts import ArtifactRef, ArtifactResolutionPolicy, ResolvedArtifact
from mote.contracts.ports import ArtifactStore


class StoreArtifactResolver:
    """Resolve through the logical index without exposing physical CAS paths."""

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    async def resolve(
        self,
        ref: ArtifactRef,
        policy: ArtifactResolutionPolicy,
    ) -> ResolvedArtifact:
        if ref.sensitivity not in policy.allowed_sensitivities:
            raise PermissionError(f"artifact sensitivity {ref.sensitivity.value!r} is not allowed")
        if ref.size > policy.max_bytes:
            raise ValueError(f"artifact size {ref.size} exceeds resolution limit {policy.max_bytes}")
        return ResolvedArtifact(ref=ref, content=await self._store.read(ref))


__all__ = ["StoreArtifactResolver"]
