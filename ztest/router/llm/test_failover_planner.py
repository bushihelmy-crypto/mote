import pytest

from mote.contracts.model.errors import ModelCapabilityUnsatisfiedError
from mote.contracts.model.invocation import CanonicalMessage, EmbeddingInput, GenerateInput, ModelInvocation
from mote.contracts.model.operations import ModelOperation
from mote.contracts.model.topology import DefaultRoute, TaskRoute
from mote.product.config.model.inputs import (
    ExplicitModelsConfig,
    ProductEndpointCapabilitiesInput,
    ProductEndpointInput,
    ProductExplicitEndpointInput,
    ProductFailoverGroupInput,
    ProductRecoveryInput,
    ProductRoutesInput,
    ShortcutModelsConfig,
)
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


def _operation_source(*, unsupported_anthropic_embedding: bool = False) -> ExplicitModelsConfig:
    anthropic_operations = {ModelOperation.GENERATE}
    if unsupported_anthropic_embedding:
        anthropic_operations.add(ModelOperation.EMBEDDING)
    return ExplicitModelsConfig(
        mode="explicit",
        endpoints={
            "chat-only": ProductExplicitEndpointInput(
                provider="anthropic",
                model="claude-sonnet-4-8",
                api_key="secret",
                capabilities=ProductEndpointCapabilitiesInput(supported_operations=frozenset(anthropic_operations)),
            ),
            "finite": ProductExplicitEndpointInput(provider="openai", model="gpt-4o", api_key="secret"),
        },
        failover_groups={
            "mixed": ProductFailoverGroupInput(endpoints=["chat-only", "finite"], recovery_profile="default")
        },
        routes=ProductRoutesInput(default="mixed"),
        recovery_profiles={"default": ProductRecoveryInput()},
    )


def _prepare(source: ExplicitModelsConfig):
    return prepare_model_generation(
        source,
        provider_catalog_revision=ProviderCatalogRevision("providers"),
        adapter_factory_revision=AdapterFactoryRevision("adapters"),
        credential_sources=_Sources(),
    )


def test_operation_admission_filters_heterogeneous_endpoints_before_wire() -> None:
    generation = _prepare(_operation_source())
    planner = FailoverPlanner(build_canonical_model_runtime_snapshot(generation.topology))
    invocation = ModelInvocation(
        model_call_id="embed-1",
        route_id=DefaultRoute(),
        task="embedding",
        operation=ModelOperation.EMBEDDING,
        input=EmbeddingInput(values=("hello",)),
    )

    assert [item.endpoint_id for item in planner.plan(invocation).endpoints] == ["finite"]

    chat_only = generation.topology.model_copy(
        update={
            "failover_groups": (
                generation.topology.failover_groups[0].model_copy(update={"endpoint_ids": ("chat-only",)}),
            )
        }
    )
    with pytest.raises(ModelCapabilityUnsatisfiedError):
        FailoverPlanner(build_canonical_model_runtime_snapshot(chat_only)).plan(invocation)


def test_product_catalog_rejects_operation_without_transport_adapter() -> None:
    with pytest.raises(ValueError, match="without a Product transport adapter"):
        _prepare(_operation_source(unsupported_anthropic_embedding=True))


def test_planner_rejects_projection_incompatible_failover_group_independent_of_order() -> None:
    generation = _prepare(_operation_source())
    first, second = generation.topology.endpoints
    second = second.model_copy(
        update={"capabilities": second.capabilities.model_copy(update={"supports_tools": False})}
    )
    invocation = ModelInvocation(
        model_call_id="generate-1",
        route_id=DefaultRoute(),
        task="generate",
        operation=ModelOperation.GENERATE,
        input=GenerateInput(messages=(CanonicalMessage(role="user", content="hello"),)),
    )

    for endpoints in ((first, second), (second, first)):
        topology = generation.topology.model_copy(update={"endpoints": endpoints})
        with pytest.raises(
            ModelCapabilityUnsatisfiedError,
            match="projection-incompatible",
        ):
            FailoverPlanner(build_canonical_model_runtime_snapshot(topology)).plan(invocation)
