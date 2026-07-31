from __future__ import annotations

import hashlib

import pytest

from mote.contracts.artifact import ArtifactContentRef
from mote.contracts.content import ContentIdentity
from mote.contracts.runtime import CheckpointFidelity, RuntimeCheckpoint
from mote.runtime.artifacts import DurableArtifactStore
from mote.runtime.interactive import ArtifactCheckpointPayloadStore
from mote.runtime.interactive.checkpoint_codec import decode_inline_json, encode_inline_json
from mote.runtime.secrets.cipher import AesGcmCipher


class _MemoryBlobs:
    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}

    def put_bytes(self, content: bytes) -> ArtifactContentRef:
        digest = hashlib.sha256(content).hexdigest()
        self.contents[digest] = content
        return ArtifactContentRef(ContentIdentity(digest, len(content)), f"sha256:{digest}")

    def read_bytes(self, ref: ArtifactContentRef) -> bytes:
        return self.contents[ref.identity.digest]


def _checkpoint(*, sensitivity: str = "private") -> RuntimeCheckpoint:
    driver = encode_inline_json(
        {"cookies": [{"name": "session", "value": "top-secret-cookie"}]},
        codec="browser-state+json@1",
        fidelity=CheckpointFidelity.LOGICAL,
        sensitivity=sensitivity,
    )
    return RuntimeCheckpoint(
        runtime_id="browser-runtime",
        kind="browser",
        alias="default",
        epoch=1,
        revision=2,
        codec=driver.codec,
        schema_version=driver.schema_version,
        payload_ref=driver.payload_ref,
        digest=driver.digest,
        sensitivity=driver.sensitivity,
        fidelity=driver.fidelity,
    )


@pytest.mark.asyncio
async def test_checkpoint_payload_is_externalized_and_reopened(tmp_path):
    blobs = _MemoryBlobs()
    artifacts = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)
    payloads = ArtifactCheckpointPayloadStore(artifacts, AesGcmCipher(b"k" * 32))

    sealed = await payloads.seal(_checkpoint())
    reopened = await payloads.open(sealed)

    assert sealed.payload_ref.startswith("artifact:runtime-checkpoint-")
    assert "base64" not in sealed.payload_ref
    assert decode_inline_json(reopened, codec="browser-state+json@1")["cookies"]


@pytest.mark.asyncio
async def test_secret_checkpoint_is_encrypted_and_idempotent(tmp_path):
    blobs = _MemoryBlobs()
    artifacts = DurableArtifactStore(tmp_path / "artifacts.sqlite3", blobs)
    payloads = ArtifactCheckpointPayloadStore(artifacts, AesGcmCipher(b"k" * 32))
    checkpoint = _checkpoint(sensitivity="secret")

    first = await payloads.seal(checkpoint)
    second = await payloads.seal(checkpoint)
    reopened = await payloads.open(first)

    assert first == second
    assert all(b"top-secret-cookie" not in content for content in blobs.contents.values())
    assert decode_inline_json(reopened, codec="browser-state+json@1")["cookies"][0]["value"] == "top-secret-cookie"
