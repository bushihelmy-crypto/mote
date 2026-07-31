from __future__ import annotations

import pickle

import pytest

from mote.product.config.model.inputs import parse_product_models_config
from mote.product.models.compiler import AdapterFactoryRevision, CredentialSourceDescriptor, ProviderCatalogRevision
from mote.product.models.generation_builder import (
    NewModelGeneration,
    ReusedModelGeneration,
    build_or_reuse_model_generation,
)
from mote.product.models.secrets import (
    CredentialEpoch,
    CredentialMaterial,
    CredentialWireAccess,
    InMemorySecretHandle,
    SecretIdentity,
)
from mote.runtime.models.failover.snapshot import build_canonical_model_runtime_snapshot


def _source() -> object:
    return parse_product_models_config(
        {
            "mode": "shortcut",
            "default": {
                "provider": "openai",
                "api_type": "openai",
                "model": "gpt-5",
                "api_key": "not-read-by-compiler",
            },
            "tasks": {
                "compression": {
                    "provider": "anthropic",
                    "api_type": "anthropic",
                    "model": "claude-sonnet-4-8",
                    "api_key": "not-read-by-compiler",
                }
            },
        }
    )


class _Catalog:
    def __init__(self, epoch: str = "one", fail_after: int | None = None) -> None:
        self.epoch = epoch
        self.fail_after = fail_after
        self.created: list[InMemorySecretHandle] = []

    def describe(self, source_ids):
        return tuple(CredentialSourceDescriptor(source_id, CredentialEpoch(self.epoch)) for source_id in source_ids)

    async def create_handle(self, slot_id, endpoint_id, source_id):
        if self.fail_after is not None and len(self.created) >= self.fail_after:
            raise RuntimeError("factory failed")
        handle = InMemorySecretHandle(
            endpoint_id=endpoint_id,
            slot_id=slot_id,
            identity=SecretIdentity(source_id),
            epoch=CredentialEpoch(self.epoch),
            value=f"wire-{source_id}",
        )
        self.created.append(handle)
        return handle


class _Current:
    def __init__(self, reuse_key) -> None:
        self._reuse_key = reuse_key
        self.retained = 0

    @property
    def reuse_key(self):
        return self._reuse_key

    def retain(self):
        self.retained += 1
        return self


async def _build(
    catalog,
    current=None,
    provider="providers-v1",
    adapter="adapter-v1",
    inference="inference-v1",
):
    return await build_or_reuse_model_generation(
        _source(),
        provider_catalog_revision=ProviderCatalogRevision(provider),
        adapter_factory_revision=AdapterFactoryRevision(adapter),
        credential_sources=catalog,
        current=current,
        inference_revision=inference,
    )


@pytest.mark.asyncio
async def test_builds_canonical_topology_and_bindings() -> None:
    catalog = _Catalog()
    result = await _build(catalog)
    assert isinstance(result, NewModelGeneration)
    assert len(result.compiled.topology.endpoints) == 2
    assert set(result.compiled.credential_bindings.handles) == {
        "endpoint:default:key:0",
        "endpoint:task:compression:key:0",
    }
    assert result.compiled.topology_revision


@pytest.mark.asyncio
async def test_runtime_snapshot_only_indexes_compiled_semantics() -> None:
    result = await _build(_Catalog())
    assert isinstance(result, NewModelGeneration)
    snapshot = build_canonical_model_runtime_snapshot(result.compiled.topology)
    assert snapshot.revision == result.compiled.topology_revision
    assert snapshot.endpoints[0].capabilities.supports_tools
    assert snapshot.slots_for_endpoint("endpoint:default") == ("endpoint:default:key:0",)


@pytest.mark.asyncio
async def test_equal_reuse_key_retains_without_creating_handles() -> None:
    first = await _build(_Catalog())
    assert isinstance(first, NewModelGeneration)
    current = _Current(first.reuse_key)
    catalog = _Catalog()
    second = await _build(catalog, current=current)
    assert isinstance(second, ReusedModelGeneration)
    assert current.retained == 1
    assert catalog.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("epoch", "provider", "adapter", "inference"),
    [
        ("two", "providers-v1", "adapter-v1", "inference-v1"),
        ("one", "providers-v2", "adapter-v1", "inference-v1"),
        ("one", "providers-v1", "adapter-v2", "inference-v1"),
        ("one", "providers-v1", "adapter-v1", "inference-v2"),
    ],
)
async def test_each_reuse_dimension_forces_rebuild(epoch, provider, adapter, inference) -> None:
    first = await _build(_Catalog())
    assert isinstance(first, NewModelGeneration)
    result = await _build(
        _Catalog(epoch),
        current=_Current(first.reuse_key),
        provider=provider,
        adapter=adapter,
        inference=inference,
    )
    assert isinstance(result, NewModelGeneration)


@pytest.mark.asyncio
async def test_partial_handle_factory_failure_closes_created_handles() -> None:
    catalog = _Catalog(fail_after=1)
    with pytest.raises(RuntimeError, match="factory failed"):
        await _build(catalog)
    assert len(catalog.created) == 1
    with pytest.raises(RuntimeError, match="closed"):
        await catalog.created[0].acquire()


def test_credential_material_is_redacted_and_access_scoped() -> None:
    material = CredentialMaterial("endpoint", "slot", "canary-secret")
    assert "canary" not in repr(material)
    assert str(material) == "<redacted>"
    with pytest.raises(TypeError):
        pickle.dumps(material)
    with pytest.raises(PermissionError):
        material.read_for_wire(CredentialWireAccess("other", "slot"))
    assert material.read_for_wire(CredentialWireAccess("endpoint", "slot")) == "canary-secret"
    material.release()
    material.release()
    with pytest.raises(RuntimeError, match="released"):
        material.read_for_wire(CredentialWireAccess("endpoint", "slot"))
