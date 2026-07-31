"""Shared daemon adapter for the application generation owner."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from mote.contracts.inference.generation_artifact import GenerationArtifact
from mote.contracts.inference.shared import SharedSessionCredential
from mote.runtime.inference.generation import GatewayGenerationOwner


class GenerationPersistence(Protocol):
    async def stage_generation(self, artifact: GenerationArtifact) -> None:
        ...

    async def activate_generation(self, generation_id: str, artifact_digest: str) -> None:
        ...


class SharedGenerationBackend:
    def __init__(
        self,
        owner: GatewayGenerationOwner,
        *,
        persistence: GenerationPersistence | None = None,
        on_activation: Callable[[], None] | None = None,
    ) -> None:
        self._owner = owner
        self._persistence = persistence
        self._on_activation = on_activation

    async def stage_generation(self, request: Any, credential: SharedSessionCredential) -> tuple[str, str, str]:
        artifact = GenerationArtifact.model_validate_json(request.generation_artifact)
        envelope = request.envelope
        if envelope.generation_id != artifact.generation_id:
            raise ValueError("generation envelope id mismatch")
        if envelope.generation_artifact_digest != artifact.artifact_digest:
            raise ValueError("generation envelope digest mismatch")
        if self._persistence is not None:
            await self._persistence.stage_generation(artifact)
        self._owner.stage(artifact)
        if artifact.activation_policy.get("activate_immediately") is True:
            if self._persistence is not None:
                await self._persistence.activate_generation(artifact.generation_id, artifact.artifact_digest)
            self._owner.activate(artifact.generation_id, artifact.artifact_digest)
            if self._on_activation is not None:
                self._on_activation()
        digest, state = self._owner.describe(artifact.generation_id)
        return artifact.generation_id, digest, state.value

    async def observe_generation(self, request: Any, credential: SharedSessionCredential) -> tuple[str, str, str]:
        generation_id = request.envelope.generation_id
        if not generation_id:
            raise ValueError("generation id is required")
        digest, state = self._owner.describe(generation_id)
        expected = request.envelope.generation_artifact_digest
        if expected and expected != digest:
            raise ValueError("generation envelope digest mismatch")
        return generation_id, digest, state.value

    async def activate_generation(self, generation_id: str, artifact_digest: str) -> tuple[str, str, str]:
        if not generation_id or not artifact_digest:
            raise ValueError("generation identity and digest are required")
        if self._persistence is not None:
            await self._persistence.activate_generation(generation_id, artifact_digest)
        self._owner.activate(generation_id, artifact_digest)
        if self._on_activation is not None:
            self._on_activation()
        digest, state = self._owner.describe(generation_id)
        return generation_id, digest, state.value
