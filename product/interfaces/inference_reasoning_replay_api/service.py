"""Tenant/session/model-bound reasoning continuity without exposing payloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from mote.contracts.artifact import ArtifactRevision
from mote.contracts.ports.artifact.store import ArtifactLookupIndex


@dataclass(frozen=True, slots=True)
class ReasoningReplayIdentity:
    tenant_id: str
    session_id: str
    provider: str
    model: str
    generation_id: str
    conversation_digest: str
    turn_ordinal: int

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.session_id, self.provider, self.model, self.generation_id)):
            raise ValueError("reasoning replay identity is incomplete")
        if len(self.conversation_digest) != 71 or not self.conversation_digest.startswith("sha256:"):
            raise ValueError("reasoning replay conversation digest is invalid")
        if self.turn_ordinal < 1:
            raise ValueError("reasoning replay turn ordinal must be positive")

    @property
    def lookup_key(self) -> str:
        canonical = json.dumps(
            {
                "conversation_digest": self.conversation_digest,
                "generation_id": self.generation_id,
                "model": self.model,
                "provider": self.provider,
                "session_id": self.session_id,
                "tenant_id": self.tenant_id,
                "turn_ordinal": self.turn_ordinal,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "sha256:" + hashlib.sha256(canonical).hexdigest()


class ReasoningReplayLookup:
    def __init__(self, index: ArtifactLookupIndex) -> None:
        self._index = index

    async def publish(self, identity: ReasoningReplayIdentity, revision: ArtifactRevision) -> None:
        if any(reference.sensitivity.value != "secret" for reference in revision.representations):
            raise ValueError("reasoning continuity artifacts must be secret")
        await self._index.publish_lookup(identity.lookup_key, revision.artifact_id, revision.revision)

    async def resolve(self, identity: ReasoningReplayIdentity) -> ArtifactRevision | None:
        return await self._index.resolve_lookup(identity.lookup_key)
