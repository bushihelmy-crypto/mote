import asyncio

from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore
from mote.product.inference.daemon.lifecycle import SharedDaemonLifecycle
from mote.runtime.inference.generation import GatewayGenerationOwner
from mote.ztest.inference.test_generation import _artifact


def test_shared_readiness_stays_closed_until_reconciled_generation_is_active(tmp_path):
    async def scenario():
        owner = GatewayGenerationOwner()
        lifecycle = SharedDaemonLifecycle(
            persistence=SQLiteAttemptReceiptStore(tmp_path / "authority.sqlite3"),
            generations=owner,
            hard_min_free_bytes=0,
        )
        result = await lifecycle.start()
        assert result.reconciled_attempts == 0
        assert lifecycle.readiness()[0] is False
        artifact = _artifact("generation-1")
        owner.stage(artifact)
        owner.activate(artifact.generation_id, artifact.artifact_digest)
        lifecycle.open_admission_after_generation_activation()
        assert lifecycle.readiness()[0] is True
        lifecycle.begin_drain()
        assert lifecycle.readiness()[0] is False
        assert lifecycle.readiness()[1]["admission"] == "draining"

    asyncio.run(scenario())


def test_shared_restart_restores_durable_active_generation_before_readiness(tmp_path):
    async def scenario():
        path = tmp_path / "authority.sqlite3"
        first_store = SQLiteAttemptReceiptStore(path)
        await first_store.initialize()
        artifact = _artifact("generation-1")
        await first_store.stage_generation(artifact)
        await first_store.activate_generation(artifact.generation_id, artifact.artifact_digest)

        restarted_owner = GatewayGenerationOwner()
        restarted = SharedDaemonLifecycle(
            persistence=SQLiteAttemptReceiptStore(path),
            generations=restarted_owner,
            hard_min_free_bytes=0,
        )
        result = await restarted.start()
        return result, restarted, restarted_owner

    result, lifecycle, owner = asyncio.run(scenario())
    assert result.components["generation"] == "ready"
    assert lifecycle.readiness()[0] is True
    assert owner.active_generation_id == "generation-1"
