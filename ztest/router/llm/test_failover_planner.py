from mote.contracts.model.topology import DefaultRoute, TaskRoute
from mote.product.config.model.inputs import ProductEndpointInput, ShortcutModelsConfig
from mote.product.models.compiler import (
    AdapterFactoryRevision,
    CredentialSourceDescriptor,
    ProviderCatalogRevision,
    prepare_model_generation,
)
from mote.product.models.secrets import CredentialEpoch
from mote.runtime.models.failover.planner import FailoverPlanner
from mote.runtime.models.failover.snapshot import build_canonical_model_runtime_snapshot


class _Sources:
    def describe(self, source_ids):
        return tuple(CredentialSourceDescriptor(source_id, CredentialEpoch("epoch")) for source_id in source_ids)


def test_planner_consumes_only_canonical_typed_routes() -> None:
    plan = prepare_model_generation(
        ShortcutModelsConfig(
            default=ProductEndpointInput(provider="openai", model="gpt-4o", api_key="secret"),
            tasks={"compression": ProductEndpointInput(provider="openai", model="gpt-4o-mini", api_key="secret")},
        ),
        provider_catalog_revision=ProviderCatalogRevision("providers"),
        adapter_factory_revision=AdapterFactoryRevision("adapters"),
        credential_sources=_Sources(),
    )
    snapshot = build_canonical_model_runtime_snapshot(plan.topology)
    planner = FailoverPlanner(snapshot)

    assert planner.snapshot.group_for_route(DefaultRoute()) is not None
    assert planner.snapshot.group_for_route(TaskRoute(name="compression")) is not None
