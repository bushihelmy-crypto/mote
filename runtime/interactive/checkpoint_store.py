"""Artifact-backed, optionally encrypted Runtime checkpoint payloads."""
from __future__ import annotations

import hashlib
import re
from dataclasses import replace

from mote.contracts.artifacts import (
    ArtifactPublishRequest,
    ArtifactRepresentationInput,
    ArtifactRetention,
    ArtifactSensitivity,
)
from mote.contracts.errors.artifacts import ArtifactNotFoundError
from mote.contracts.ports import ArtifactStore
from mote.contracts.runtimes import RuntimeCheckpoint
from mote.runtime.interactive.checkpoint_codec import decode_inline_bytes, inline_checkpoint
from mote.runtime.secrets.cipher import VaultCipher

_ARTIFACT_REF = re.compile(
    r"^artifact:(?P<artifact_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})"
    r"@(?P<revision>[1-9][0-9]*):(?P<representation>[a-z0-9][a-z0-9._-]{0,63})$"
)


class ArtifactCheckpointPayloadStore:
    """Move checkpoint bytes out of rollout and encrypt secret payloads."""

    def __init__(self, artifacts: ArtifactStore, cipher: VaultCipher) -> None:
        self._artifacts = artifacts
        self._cipher = cipher

    async def seal(self, checkpoint: RuntimeCheckpoint) -> RuntimeCheckpoint:
        if checkpoint.payload_ref.startswith("artifact:"):
            return checkpoint
        raw = decode_inline_bytes(checkpoint)
        sensitivity = ArtifactSensitivity(checkpoint.sensitivity)
        secret = sensitivity is ArtifactSensitivity.SECRET
        content = self._cipher.encrypt(raw) if secret else raw
        representation = "encrypted" if secret else "json"
        identity = hashlib.sha256(
            (
                f"{checkpoint.runtime_id}\0{checkpoint.kind}\0{checkpoint.epoch}\0"
                f"{checkpoint.revision}\0{checkpoint.codec}\0{checkpoint.digest}"
            ).encode("utf-8")
        ).hexdigest()
        artifact_id = f"runtime-checkpoint-{identity}"
        try:
            existing = await self._artifacts.get_revision(artifact_id, 1)
        except ArtifactNotFoundError:
            existing = None
        if existing is not None:
            if existing.get(representation).sensitivity.value != checkpoint.sensitivity:
                raise ValueError("checkpoint Artifact sensitivity does not match")
            return replace(
                checkpoint,
                payload_ref=f"artifact:{artifact_id}@1:{representation}",
            )
        revision = await self._artifacts.publish(
            ArtifactPublishRequest(
                artifact_id=artifact_id,
                expected_revision=0,
                idempotency_key=artifact_id,
                retention=ArtifactRetention.SESSION,
                sensitivity=sensitivity,
                representations=(
                    ArtifactRepresentationInput(
                        representation=representation,
                        kind="runtime-checkpoint",
                        mime_type=("application/octet-stream" if secret else "application/json"),
                        content=content,
                        suggested_name="",
                    ),
                ),
            )
        )
        return replace(
            checkpoint,
            payload_ref=(f"artifact:{artifact_id}@{revision.revision}:{representation}"),
        )

    async def open(self, checkpoint: RuntimeCheckpoint) -> RuntimeCheckpoint:
        if checkpoint.payload_ref.startswith("data:"):
            return checkpoint
        match = _ARTIFACT_REF.fullmatch(checkpoint.payload_ref)
        if match is None:
            raise ValueError("checkpoint payload reference is unsupported")
        artifact = await self._artifacts.get_revision(match.group("artifact_id"), int(match.group("revision")))
        ref = artifact.get(match.group("representation"))
        expected_sensitivity = ArtifactSensitivity(checkpoint.sensitivity)
        if ref.sensitivity is not expected_sensitivity:
            raise ValueError("checkpoint Artifact sensitivity does not match")
        content = await self._artifacts.read(ref)
        raw = self._cipher.decrypt(content) if checkpoint.sensitivity == ArtifactSensitivity.SECRET.value else content
        return inline_checkpoint(checkpoint, raw)


__all__ = ["ArtifactCheckpointPayloadStore"]
