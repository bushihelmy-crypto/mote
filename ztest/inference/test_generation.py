import asyncio
import json

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
from mote.product.inference.backends.sqlite import ReceiptConflictError, SQLiteAttemptReceiptStore
from mote.product.inference.daemon.generation import SharedGenerationBackend
from mote.runtime.inference.generation import GatewayGenerationOwner, GenerationDomain, GenerationState


def _artifact(generation, parent=None):
    digest = "sha256:" + generation[-1] * 64
    artifact = GenerationArtifact(
        generation_id=generation,
        parent_generation_id=parent,
        model_binding=ModelGenerationBinding(topology_revision=generation),
        service_binding=ServiceGenerationBinding(runtime=RuntimeBindingKind.EMBEDDED, configured=True),
        session_binding=SessionGenerationBinding(runtime=RuntimeBindingKind.EMBEDDED, configured=True),
        transfer_binding=TransferGenerationBinding(runtime=RuntimeBindingKind.EMBEDDED, configured=True),
        credential_versions=(VersionBinding(identity="slot", revision="1"),),
        transport_registry_revision="transport-v1",
        client_profile_revision=f"client-{generation[-1]}",
        failure_policy_revision="failure-v2",
        capability_pricing=CapabilityPricingSnapshot(catalog_revision="catalog", pricing_revision="pricing"),
        governance_plugins=(),
        required_wire_contract_range=(1, 1),
        activation_policy=GenerationActivationPolicy(deployment=DeploymentKind.EMBEDDED, activate_immediately=False),
        min_reader_version=1,
        min_writer_version=1,
        persistence_schemas=(VersionBinding(identity="receipt", revision="1"),),
        migration_set_digest="sha256:" + "f" * 64,
        artifact_digest=digest,
    )
    return artifact.model_copy(update={"artifact_digest": compute_generation_artifact_digest(artifact)})


def test_activation_is_atomic_and_old_generation_retires_after_last_reference():
    owner = GatewayGenerationOwner()
    first = _artifact("generation-1")
    second = _artifact("generation-2", "generation-1")
    owner.stage(first)
    owner.activate(first.generation_id, first.artifact_digest)
    lease = owner.acquire(GenerationDomain.MODEL)
    owner.stage(second)
    owner.activate(second.generation_id, second.artifact_digest)
    assert owner.active_generation_id == second.generation_id
    assert owner.state(first.generation_id) is GenerationState.DRAINING
    assert lease.model_view().bindings["topology_revision"] == "generation-1"
    with pytest.raises(PermissionError):
        lease.service_view()
    lease.release()
    assert owner.state(first.generation_id) is GenerationState.RETIRED


def test_digest_mismatch_does_not_disturb_active_generation():
    owner = GatewayGenerationOwner()
    first = _artifact("generation-1")
    second = _artifact("generation-2")
    owner.stage(first)
    owner.activate(first.generation_id, first.artifact_digest)
    owner.stage(second)
    with pytest.raises(ValueError, match="digest mismatch"):
        owner.activate(second.generation_id, "sha256:" + "0" * 64)
    assert owner.active_generation_id == first.generation_id
    assert owner.state(first.generation_id) is GenerationState.ACTIVE


def test_stage_rejects_typed_binding_tamper_under_stale_content_digest():
    artifact = _artifact("generation-1")
    tampered = artifact.model_copy(
        update={"model_binding": artifact.model_binding.model_copy(update={"topology_revision": "tampered"})}
    )
    with pytest.raises(ValueError, match="content digest mismatch"):
        GatewayGenerationOwner().stage(tampered)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: {**value, "schema_version": 1},
        lambda value: {**value, "extra": True},
        lambda value: {**value, "model_binding": {**value["model_binding"], "variant": "unknown"}},
        lambda value: {**value, "activation_policy": {**value["activation_policy"], "activate_immediately": 1}},
    ),
)
def test_generation_artifact_strict_reader_rejects_unknown_or_wrong_shape(mutate):
    payload = json.loads(_artifact("generation-1").model_dump_json())
    with pytest.raises(ValueError):
        GenerationArtifact.model_validate(mutate(payload), strict=True)


@pytest.mark.parametrize(
    "updates",
    (
        {"required_wire_contract_range": (3, 2)},
        {"required_wire_contract_range": (2, 3), "min_reader_version": 3},
        {"required_wire_contract_range": (2, 3), "min_writer_version": 3},
    ),
)
def test_generation_artifact_rejects_incoherent_wire_version_bounds(updates):
    payload = _artifact("generation-1").model_dump()
    payload.update(updates)

    with pytest.raises(ValueError, match="wire contract range|reader/writer"):
        GenerationArtifact.model_validate(payload)


def test_retired_client_profile_decoder_remains_pinned_for_open_resume():
    owner = GatewayGenerationOwner()
    first = _artifact("generation-1")
    second = _artifact("generation-2", "generation-1")
    owner.stage(first)
    owner.activate(first.generation_id, first.artifact_digest)
    resume_lease = owner.acquire(GenerationDomain.MODEL)

    owner.stage(second)
    owner.activate(second.generation_id, second.artifact_digest)

    assert owner.state(first.generation_id) is GenerationState.DRAINING
    assert resume_lease.model_view().client_profile_revision == "client-1"
    active_lease = owner.acquire(GenerationDomain.MODEL)
    assert active_lease.model_view().client_profile_revision == "client-2"
    active_lease.release()
    resume_lease.release()
    assert owner.state(first.generation_id) is GenerationState.RETIRED


def test_unknown_generation_is_rejected_without_changing_active_generation():
    owner = GatewayGenerationOwner()
    first = _artifact("generation-1")
    owner.stage(first)
    owner.activate(first.generation_id, first.artifact_digest)
    with pytest.raises(KeyError, match="unknown generation"):
        owner.activate("generation-9", "sha256:" + "9" * 64)
    assert owner.active_generation_id == first.generation_id


def test_draining_generation_cannot_be_reactivated():
    owner = GatewayGenerationOwner()
    first = _artifact("generation-1")
    second = _artifact("generation-2", "generation-1")
    owner.stage(first)
    owner.activate(first.generation_id, first.artifact_digest)
    lease = owner.acquire(GenerationDomain.MODEL)
    owner.stage(second)
    owner.activate(second.generation_id, second.artifact_digest)

    with pytest.raises(ValueError, match="cannot activate from draining"):
        owner.activate(first.generation_id, first.artifact_digest)

    assert owner.active_generation_id == second.generation_id
    lease.release()


def test_shared_generation_backend_activates_only_exact_staged_digest():
    async def scenario():
        owner = GatewayGenerationOwner()
        artifact = _artifact("generation-1")
        owner.stage(artifact)
        activations = []
        backend = SharedGenerationBackend(owner, on_activation=lambda: activations.append(True))
        result = await backend.activate_generation(artifact.generation_id, artifact.artifact_digest)
        with pytest.raises(ValueError, match="digest mismatch"):
            await backend.activate_generation(artifact.generation_id, "sha256:" + "b" * 64)
        return result, activations

    result, activations = asyncio.run(scenario())
    assert result[2] == "active"
    assert activations == [True]


def test_persistent_draining_generation_cannot_be_reactivated(tmp_path):
    async def scenario():
        store = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await store.initialize()
        first = _artifact("generation-1")
        second = _artifact("generation-2", "generation-1")
        await store.stage_generation(first)
        await store.activate_generation(first.generation_id, first.artifact_digest)
        await store.stage_generation(second)
        await store.activate_generation(second.generation_id, second.artifact_digest)
        with pytest.raises(ReceiptConflictError, match="cannot activate from draining"):
            await store.activate_generation(first.generation_id, first.artifact_digest)

    asyncio.run(scenario())
