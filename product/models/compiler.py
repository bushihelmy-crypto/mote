"""Single Product compiler from user model syntax to canonical topology."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Protocol

from mote.contracts.model.execution_policy import EndpointExecutionPolicy
from mote.contracts.model.operations import ModelOperation
from mote.contracts.model.profile import profile_for
from mote.contracts.model.topology import (
    DefaultRoute,
    EndpointCapabilityDeclaration,
    FailoverGroupTopology,
    ModelEndpointTopology,
    ModelTopology,
    RecoveryProfileTopology,
    RouteBinding,
    SemanticRoute,
    TaskRoute,
)
from mote.contracts.model.topology_codec import canonical_base_url, topology_revision
from mote.product.config.model.inputs import (
    ProductEndpointCapabilitiesInput,
    ProductEndpointInput,
    ProductModelsConfig,
    ProductRecoveryInput,
    ShortcutModelsConfig,
)
from mote.product.models.secrets import CredentialEpoch, SecretHandle

_CONTEXT_WINDOWS = {
    "claude": 200_000,
    "deepseek": 128_000,
    "gpt-4": 128_000,
    "gpt-5": 400_000,
}

_GENERATE_TRANSPORTS = frozenset(
    {
        "anthropic",
        "anthropic_messages",
        "bedrock",
        "aws_bedrock",
        "google",
        "gemini",
        "google_generate_content",
        "vertex",
        "openai",
        "openai_chat",
        "openai_responses",
        "azure",
        "openrouter",
        "vllm",
        "xai",
    }
)
_OPENAI_FINITE_TRANSPORTS = frozenset(
    {"openai", "openai_chat", "openai_responses", "azure", "openrouter", "vllm", "xai"}
)
_GOOGLE_FINITE_TRANSPORTS = frozenset({"google", "gemini", "google_generate_content", "vertex"})


def supported_operations_for_transport(transport: str) -> frozenset[ModelOperation]:
    """Return the exact operation set backed by Product transport factories."""
    normalized = transport.lower()
    operations: set[ModelOperation] = set()
    if normalized in _GENERATE_TRANSPORTS:
        operations.add(ModelOperation.GENERATE)
    if normalized in _OPENAI_FINITE_TRANSPORTS:
        operations.update(
            {
                ModelOperation.EMBEDDING,
                ModelOperation.IMAGE_GENERATION,
                ModelOperation.SPEECH,
                ModelOperation.TRANSCRIPTION,
            }
        )
    elif normalized in _GOOGLE_FINITE_TRANSPORTS:
        operations.update({ModelOperation.EMBEDDING, ModelOperation.IMAGE_GENERATION})
    if not operations:
        raise ValueError(f"unsupported model transport {transport!r}")
    return frozenset(operations)


def context_tokens_for_model(model: str | None) -> int | None:
    """Product-owned catalog lookup used only while interpreting provider input."""
    lowered = (model or "").lower()
    marker = next((key for key in _CONTEXT_WINDOWS if key in lowered), None)
    return _CONTEXT_WINDOWS[marker] if marker is not None else None


@dataclass(frozen=True, slots=True)
class ProviderCatalogRevision:
    value: str


@dataclass(frozen=True, slots=True)
class AdapterFactoryRevision:
    value: str


@dataclass(frozen=True, slots=True)
class ModelGenerationReuseKey:
    topology_revision: str
    credential_epoch: CredentialEpoch
    provider_catalog_revision: ProviderCatalogRevision
    adapter_factory_revision: AdapterFactoryRevision


@dataclass(frozen=True, slots=True)
class CredentialSourceDescriptor:
    source_id: str
    epoch: CredentialEpoch


class CredentialSourceCatalog(Protocol):
    def describe(self, source_ids: tuple[str, ...]) -> tuple[CredentialSourceDescriptor, ...]: ...

    async def create_handle(self, slot_id: str, endpoint_id: str, source_id: str) -> SecretHandle: ...


@dataclass(frozen=True, slots=True)
class PublicModelGenerationPlan:
    topology: ModelTopology
    topology_revision: str
    slots: tuple[tuple[str, str, str], ...]
    reuse_key: ModelGenerationReuseKey


@dataclass(frozen=True, slots=True)
class CredentialBindingSpec:
    handles: Mapping[str, SecretHandle]

    def __repr__(self) -> str:
        return f"CredentialBindingSpec(slots={tuple(sorted(self.handles))!r})"

    def __reduce__(self):
        raise TypeError("CredentialBindingSpec cannot be pickled")


@dataclass(frozen=True, slots=True)
class CompiledModelGeneration:
    topology: ModelTopology
    topology_revision: str
    credential_bindings: CredentialBindingSpec
    reuse_key: ModelGenerationReuseKey


def _transport(endpoint: ProductEndpointInput) -> str:
    if endpoint.api_type:
        return str(endpoint.api_type)
    if endpoint.provider:
        return endpoint.provider
    raise ValueError("model endpoint requires explicit provider or api_type")


def _capabilities(
    endpoint: ProductEndpointInput,
    explicit: ProductEndpointCapabilitiesInput | None,
) -> EndpointCapabilityDeclaration:
    profile = profile_for(endpoint.model)
    inferred_context_tokens = context_tokens_for_model(endpoint.model)
    known = inferred_context_tokens is not None
    required = (
        "supports_tools",
        "supports_native_schema",
        "supports_server_web_search",
        "supports_vision",
        "supports_pdf",
        "supports_native_tool_search",
        "context_tokens",
    )
    if explicit is None and not known:
        raise ValueError(f"unknown shortcut model {endpoint.model!r}; use explicit mode with capabilities")
    if explicit is not None and not known:
        missing = [name for name in required if getattr(explicit, name) is None]
        if missing:
            raise ValueError(f"unknown explicit model {endpoint.model!r} requires capabilities {missing!r}")

    def choose(name: str, inferred: bool) -> bool:
        value = getattr(explicit, name, None) if explicit is not None else None
        return inferred if value is None else value

    context_tokens = getattr(explicit, "context_tokens", None) if explicit is not None else None
    if context_tokens is None:
        context_tokens = inferred_context_tokens or 0
    adapter_operations = supported_operations_for_transport(_transport(endpoint))
    declared_operations = (
        explicit.supported_operations
        if explicit is not None and explicit.supported_operations is not None
        else adapter_operations
    )
    unsupported = declared_operations - adapter_operations
    if unsupported:
        raise ValueError(
            "endpoint declares operations without a Product transport adapter: "
            f"{sorted(operation.value for operation in unsupported)!r}"
        )
    return EndpointCapabilityDeclaration(
        supported_operations=declared_operations,
        supports_tools=choose("supports_tools", True),
        supports_native_schema=choose("supports_native_schema", profile.supports_native_structured_output),
        supports_server_web_search=choose("supports_server_web_search", profile.supports_web_search),
        supports_vision=choose("supports_vision", profile.supports_vision),
        supports_pdf=choose("supports_pdf", profile.supports_pdf_input),
        supports_native_tool_search=choose("supports_native_tool_search", profile.supports_native_tool_search),
        context_tokens=context_tokens,
    )


def _execution_policy(endpoint: ProductEndpointInput) -> EndpointExecutionPolicy:
    return EndpointExecutionPolicy(
        max_output_tokens=endpoint.max_token,
        temperature_micros=round(endpoint.temperature * 1_000_000),
        timeout_milliseconds=endpoint.timeout * 1000,
        reasoning_effort=endpoint.reasoning_effort,
        calculate_usage=endpoint.calc_usage,
        prompt_cache_enabled=endpoint.use_prompt_cache,
    )


def _profile(profile_id: str, value: ProductRecoveryInput) -> RecoveryProfileTopology:
    return RecoveryProfileTopology(profile_id=profile_id, **value.model_dump())


def _source_epoch(descriptors: tuple[CredentialSourceDescriptor, ...]) -> CredentialEpoch:
    payload = "\0".join(
        f"{item.source_id}\0{item.epoch.value}" for item in sorted(descriptors, key=lambda item: item.source_id)
    )
    return CredentialEpoch(hashlib.sha256(payload.encode("utf-8")).hexdigest())


def _endpoint_slots(endpoint_id: str, endpoint: ProductEndpointInput) -> tuple[tuple[str, str, str], ...]:
    if endpoint.oauth is not None:
        return (
            (f"{endpoint_id}:oauth-current", endpoint_id, f"{endpoint_id}:oauth"),
            (f"{endpoint_id}:oauth-refresh", endpoint_id, f"{endpoint_id}:oauth"),
        )
    keys = endpoint.api_key if isinstance(endpoint.api_key, list) else [endpoint.api_key]
    return tuple(
        (f"{endpoint_id}:key:{index}", endpoint_id, f"{endpoint_id}:key:{index}") for index, _ in enumerate(keys)
    )


def _inherit_shortcut_endpoint(default: ProductEndpointInput, endpoint: ProductEndpointInput) -> ProductEndpointInput:
    inherited = endpoint.model_dump()
    for field in ("provider", "api_key", "api_type", "base_url", "oauth"):
        if inherited[field] is None:
            inherited[field] = getattr(default, field)
    return ProductEndpointInput.model_validate(inherited)


def prepare_model_generation(
    source: ProductModelsConfig,
    *,
    provider_catalog_revision: ProviderCatalogRevision,
    adapter_factory_revision: AdapterFactoryRevision,
    credential_sources: CredentialSourceCatalog,
) -> PublicModelGenerationPlan:
    endpoints: list[ModelEndpointTopology] = []
    groups: list[FailoverGroupTopology] = []
    profiles: list[RecoveryProfileTopology] = []
    routes: list[RouteBinding] = []
    slots: list[tuple[str, str, str]] = []
    if isinstance(source, ShortcutModelsConfig):
        endpoint_inputs = {
            "endpoint:default": source.default,
            **{
                f"endpoint:task:{key}": _inherit_shortcut_endpoint(source.default, value)
                for key, value in source.tasks.items()
            },
        }
        profiles.append(_profile("profile:default", source.recovery_defaults))
        compression = source.recovery_defaults.model_copy(update={"max_request_transforms": 0})
        profiles.append(_profile("profile:task:compression", compression))
        for endpoint_id, endpoint in endpoint_inputs.items():
            endpoint_slots = _endpoint_slots(endpoint_id, endpoint)
            slots.extend(endpoint_slots)
            endpoints.append(
                ModelEndpointTopology(
                    endpoint_id=endpoint_id,
                    transport=_transport(endpoint),
                    provider=endpoint.provider or _transport(endpoint),
                    model=endpoint.model,
                    base_url=canonical_base_url(endpoint.base_url or "https://api.openai.com/v1"),
                    capabilities=_capabilities(endpoint, None),
                    governance_domain="default",
                    region="global",
                    pricing_class="default",
                    credential_slots=tuple(slot for slot, _, _ in endpoint_slots),
                    execution_policy=_execution_policy(endpoint),
                )
            )
            group_id = f"group:{endpoint_id}"
            profile_id = "profile:task:compression" if endpoint_id == "endpoint:task:compression" else "profile:default"
            groups.append(
                FailoverGroupTopology(
                    group_id=group_id,
                    endpoint_ids=(endpoint_id,),
                    recovery_profile_id=profile_id,
                )
            )
        routes.append(RouteBinding(route_id=DefaultRoute(), group_id="group:endpoint:default"))
        routes.extend(
            RouteBinding(route_id=TaskRoute(name=task), group_id=f"group:endpoint:task:{task}") for task in source.tasks
        )
    else:
        for profile_id, value in source.recovery_profiles.items():
            profiles.append(_profile(profile_id, value))
        for endpoint_id, endpoint in source.endpoints.items():
            if endpoint.credential_pool is not None:
                pool = source.credential_pools[endpoint.credential_pool]
                endpoint_slots = tuple((slot.id, endpoint_id, slot.secret_ref) for slot in pool.slots)
            else:
                endpoint_slots = _endpoint_slots(endpoint_id, endpoint)
            slots.extend(endpoint_slots)
            endpoints.append(
                ModelEndpointTopology(
                    endpoint_id=endpoint_id,
                    transport=_transport(endpoint),
                    provider=endpoint.provider or _transport(endpoint),
                    model=endpoint.model,
                    base_url=canonical_base_url(endpoint.base_url or "https://api.openai.com/v1"),
                    capabilities=_capabilities(endpoint, endpoint.capabilities),
                    governance_domain=endpoint.governance_domain,
                    region=endpoint.region,
                    pricing_class=endpoint.pricing_class,
                    credential_slots=tuple(slot for slot, _, _ in endpoint_slots),
                    execution_policy=_execution_policy(endpoint),
                )
            )
        groups.extend(
            FailoverGroupTopology(
                group_id=group_id,
                endpoint_ids=tuple(group.endpoints),
                recovery_profile_id=group.recovery_profile,
            )
            for group_id, group in source.failover_groups.items()
        )
        routes.append(RouteBinding(route_id=DefaultRoute(), group_id=source.routes.default))
        routes.extend(
            RouteBinding(route_id=TaskRoute(name=name), group_id=group) for name, group in source.routes.tasks.items()
        )
        routes.extend(
            RouteBinding(route_id=SemanticRoute(name=name), group_id=group)
            for name, group in source.routes.semantic.items()
        )
    topology = ModelTopology(
        endpoints=tuple(endpoints),
        recovery_profiles=tuple(profiles),
        failover_groups=tuple(groups),
        routes=tuple(routes),
    )
    revision = topology_revision(topology)
    source_ids = tuple(source_id for _, _, source_id in slots)
    epoch = _source_epoch(credential_sources.describe(source_ids))
    return PublicModelGenerationPlan(
        topology=topology,
        topology_revision=revision,
        slots=tuple(slots),
        reuse_key=ModelGenerationReuseKey(revision, epoch, provider_catalog_revision, adapter_factory_revision),
    )


def compile_model_generation(
    plan: PublicModelGenerationPlan,
    secret_handles: Mapping[str, SecretHandle],
) -> CompiledModelGeneration:
    expected = {slot for slot, _, _ in plan.slots}
    if set(secret_handles) != expected:
        raise ValueError("credential bindings do not match topology slots")
    return CompiledModelGeneration(
        plan.topology,
        plan.topology_revision,
        CredentialBindingSpec(dict(secret_handles)),
        plan.reuse_key,
    )


__all__ = [
    "AdapterFactoryRevision",
    "CompiledModelGeneration",
    "CredentialBindingSpec",
    "CredentialSourceCatalog",
    "CredentialSourceDescriptor",
    "ModelGenerationReuseKey",
    "ProviderCatalogRevision",
    "PublicModelGenerationPlan",
    "compile_model_generation",
    "prepare_model_generation",
    "supported_operations_for_transport",
]
