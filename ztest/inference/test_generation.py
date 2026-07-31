import asyncio

import pytest

from mote.contracts.inference.generation_artifact import GenerationArtifact
from mote.product.inference.daemon.generation import SharedGenerationBackend
from mote.runtime.inference.generation import GatewayGenerationOwner, GenerationDomain, GenerationState


def _artifact(generation, parent=None):
    digest = "sha256:" + generation[-1] * 64
    return GenerationArtifact(
        generation_id=generation,
        parent_generation_id=parent,
        model_planner_and_bindings={"model": generation},
        service_planner_and_bindings={"service": generation},
        session_capability_and_bindings={"session": generation},
        transfer_capability_and_bindings={"transfer": generation},
        credential_versions={"slot": "1"},
        transport_registry_revision="transport-v1",
        client_profile_revision=f"client-{generation[-1]}",
        failure_policy_revision="failure-v2",
        capability_catalog_pricing_snapshot={},
        governance_cache_plugin_revisions={},
        required_wire_contract_range=(1, 1),
        activation_policy={},
        min_reader_version=1,
        min_writer_version=1,
        persistence_schema_versions={"receipt": 1},
        migration_set_digest="sha256:" + "f" * 64,
        artifact_digest=digest,
        signer_key_id="key",
        signature="signature",
    )


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
    assert lease.model_view().bindings["model"] == "generation-1"
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
