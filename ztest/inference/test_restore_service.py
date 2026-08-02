import asyncio

import pytest

from mote.contracts.inference.generation_artifact import (
    CapabilityPricingSnapshot,
    DeploymentKind,
    GenerationActivationPolicy,
    GenerationArtifact,
    ModelGenerationBinding,
    RuntimeBindingKind,
    ServiceGenerationBinding,
    SessionGenerationBinding,
    TransferGenerationBinding,
    VersionBinding,
    compute_generation_artifact_digest,
)
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore
from mote.product.inference.restore import IsolatedSQLiteRestoreService, RestoreApproval


def test_restore_requires_stopped_daemon_empty_directory_and_digest_approval(tmp_path):
    async def scenario():
        audit = []

        async def record(operation, outcome, details):
            audit.append((operation, outcome, details))

        authority = tmp_path / "source" / "gateway.sqlite3"
        authority.parent.mkdir()
        source_store = SQLiteAttemptReceiptStore(authority)
        await source_store.initialize()
        artifact = _artifact("generation-1")
        await source_store.stage_generation(artifact)
        await source_store.activate_generation(artifact.generation_id, artifact.artifact_digest)
        backup = tmp_path / "backup.sqlite3"
        await source_store.backup_to(backup)
        digest = await source_store.verify_backup(backup)

        target = tmp_path / "restore"
        target.mkdir()
        running = IsolatedSQLiteRestoreService(daemon_is_stopped=lambda: False, audit=record)
        with pytest.raises(RuntimeError, match="stopped daemon"):
            await running.apply(
                backup,
                target,
                authority_name="gateway.sqlite3",
                approval=RestoreApproval("approval", digest),
            )

        service = IsolatedSQLiteRestoreService(daemon_is_stopped=lambda: True, audit=record)
        with pytest.raises(PermissionError, match="does not match"):
            await service.apply(
                backup,
                target,
                authority_name="gateway.sqlite3",
                approval=RestoreApproval("approval", "sha256:" + "0" * 64),
            )
        with pytest.raises(PermissionError, match="does not match"):
            await service.apply(
                backup,
                target,
                authority_name="gateway.sqlite3",
                approval=RestoreApproval(
                    "approval",
                    digest,
                    approved_source_generation=2,
                ),
            )
        assert list(target.iterdir()) == []

        async def broken_audit(operation, outcome, details):
            raise RuntimeError("audit unavailable")

        fail_closed = IsolatedSQLiteRestoreService(daemon_is_stopped=lambda: True, audit=broken_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await fail_closed.apply(
                backup,
                target,
                authority_name="gateway.sqlite3",
                approval=RestoreApproval("approval", digest),
            )
        assert list(target.iterdir()) == []

        result = await service.apply(
            backup,
            target,
            authority_name="gateway.sqlite3",
            approval=RestoreApproval("approval", digest),
        )
        assert result.backup_digest == digest
        assert result.metadata.logical_store == "inference-gateway-authority"
        assert result.metadata.cutover_unit_id == "inference-gateway-sqlite-v1"
        assert result.authority_path.is_file()
        assert await source_store.verify_backup(result.authority_path) == digest
        assert audit == [
            (
                "restore_apply",
                "committed",
                {
                    "approval_id": "approval",
                    "backup_digest": digest,
                    "authority_name": "gateway.sqlite3",
                    "logical_store": "inference-gateway-authority",
                    "cutover_unit_id": "inference-gateway-sqlite-v1",
                    "source_generation": "1",
                    "storage_format_version": "1",
                    "high_water_mark": result.metadata.high_water_mark,
                },
            )
        ]

        with pytest.raises(ValueError, match="must be empty"):
            await service.apply(
                backup,
                target,
                authority_name="second.sqlite3",
                approval=RestoreApproval("approval", digest),
            )

    asyncio.run(scenario())


def test_restore_service_requires_audit_authority():
    with pytest.raises(ValueError, match="audit authority"):
        IsolatedSQLiteRestoreService(daemon_is_stopped=lambda: True, audit=None)


def _artifact(generation):
    artifact = GenerationArtifact(
        generation_id=generation,
        model_binding=ModelGenerationBinding(topology_revision="topology"),
        service_binding=ServiceGenerationBinding(runtime=RuntimeBindingKind.EMBEDDED, configured=True),
        session_binding=SessionGenerationBinding(runtime=RuntimeBindingKind.EMBEDDED, configured=True),
        transfer_binding=TransferGenerationBinding(runtime=RuntimeBindingKind.EMBEDDED, configured=True),
        credential_versions=(),
        transport_registry_revision="transport-v1",
        client_profile_revision="client-v1",
        failure_policy_revision="failure-v1",
        capability_pricing=CapabilityPricingSnapshot(catalog_revision="catalog", pricing_revision="pricing"),
        governance_plugins=(),
        required_wire_contract_range=(1, 1),
        activation_policy=GenerationActivationPolicy(deployment=DeploymentKind.EMBEDDED, activate_immediately=False),
        min_reader_version=1,
        min_writer_version=1,
        persistence_schemas=(VersionBinding(identity="receipt", revision="1"),),
        migration_set_digest="sha256:" + "f" * 64,
        artifact_digest="sha256:" + "1" * 64,
    )
    return artifact.model_copy(update={"artifact_digest": compute_generation_artifact_digest(artifact)})
