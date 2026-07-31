import pytest

from mote.contracts.artifact import (
    ArtifactPublishRequest,
    ArtifactRepresentationInput,
    ArtifactRetention,
    ArtifactSensitivity,
)
from mote.product.interfaces.inference_reasoning_replay_api import ReasoningReplayIdentity, ReasoningReplayLookup
from mote.runtime.artifacts.store import DurableArtifactStore
from mote.ztest.runtime.test_artifact_store import MemoryBlobs


def _identity(**changes):
    values = {
        "tenant_id": "tenant",
        "session_id": "session",
        "provider": "openai",
        "model": "model",
        "generation_id": "generation",
        "conversation_digest": "sha256:" + "a" * 64,
        "turn_ordinal": 1,
    }
    values.update(changes)
    return ReasoningReplayIdentity(**values)


@pytest.mark.asyncio
async def test_reasoning_replay_is_strongly_scoped_and_payload_free(tmp_path):
    store = DurableArtifactStore(tmp_path / "artifacts.sqlite3", MemoryBlobs())
    revision = await store.publish(
        ArtifactPublishRequest(
            idempotency_key="reasoning-1",
            retention=ArtifactRetention.SESSION,
            sensitivity=ArtifactSensitivity.SECRET,
            representations=(
                ArtifactRepresentationInput(
                    representation="opaque",
                    kind="reasoning",
                    mime_type="application/octet-stream",
                    content=b"opaque-provider-material",
                ),
            ),
        )
    )
    replay = ReasoningReplayLookup(store)
    identity = _identity()
    await replay.publish(identity, revision)
    assert await replay.resolve(identity) == revision
    assert await replay.resolve(_identity(tenant_id="other")) is None
    assert await replay.resolve(_identity(session_id="other")) is None
    assert await replay.resolve(_identity(model="other")) is None
    assert not hasattr(replay, "read")
