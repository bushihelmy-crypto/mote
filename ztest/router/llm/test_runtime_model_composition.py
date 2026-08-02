import asyncio

import pytest

from mote.contracts.runtime.application import RuntimeGenerationId
from mote.product.config.model.inputs import ProductEndpointInput, ShortcutModelsConfig
from mote.product.models.compiler import (
    AdapterFactoryRevision,
    CredentialSourceDescriptor,
    ProviderCatalogRevision,
    prepare_model_generation,
)
from mote.product.models.secrets import CredentialEpoch
from mote.runtime.models.composition import LeaseReleasedError, build_runtime_composition
from mote.runtime.models.failover.planner import FailoverPlanner
from mote.runtime.models.failover.runtime_state import ModelRuntimeGeneration
from mote.runtime.models.failover.snapshot import build_canonical_model_runtime_snapshot
from mote.runtime.models.model_gateway import RuntimeModelGateway


class _Resolver:
    def __init__(self) -> None:
        self.closed = 0

    def resolve(self, endpoint, credential_slot_id):
        return None

    async def aclose(self) -> None:
        self.closed += 1


class _CredentialSources:
    def describe(self, source_ids):
        return tuple(CredentialSourceDescriptor(source_id, CredentialEpoch("one")) for source_id in source_ids)


def test_shared_composition_fences_gateway_and_drains() -> None:
    async def scenario() -> None:
        plan = prepare_model_generation(
            ShortcutModelsConfig(default=ProductEndpointInput(provider="openai", model="gpt-4o", api_key="secret")),
            provider_catalog_revision=ProviderCatalogRevision("providers"),
            adapter_factory_revision=AdapterFactoryRevision("adapters"),
            credential_sources=_CredentialSources(),
        )
        snapshot = build_canonical_model_runtime_snapshot(plan.topology)
        planner = FailoverPlanner(snapshot)
        resolver = _Resolver()
        executor = RuntimeModelGateway(planner)
        generation = ModelRuntimeGeneration(planner, closeables=(resolver,))
        handle = build_runtime_composition(
            runtime_generation_id=RuntimeGenerationId("runtime-one"),
            executor=executor,
            generation=generation,
            gateway_decorator=None,
            reuse_key="runtime-one",
        )
        lease = await handle.acquire()
        assert lease.runtime_generation_id == RuntimeGenerationId("runtime-one")
        assert lease.default_model.model
        await handle.release()
        assert resolver.closed == 0
        await lease.aclose()
        assert resolver.closed == 1
        with pytest.raises(LeaseReleasedError):
            _ = lease.gateway

    asyncio.run(scenario())
