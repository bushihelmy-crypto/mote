"""Product assembly from model syntax to an installable application candidate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, cast
from uuid import uuid4

from mote.contracts.config.inference import DeploymentMode
from mote.contracts.events.application import ApplicationReadinessFailed
from mote.contracts.runtime.application import (
    ReloadSequence,
    RuntimeGenerationId,
    RuntimeRoleConfigView,
    SourceRevision,
)
from mote.product.composition.model_application import ApplicationCompositionCandidate, SharedRuntimeCompositionHandle
from mote.product.config.schema import Config
from mote.product.models.artifacts import ProductInferenceArtifacts
from mote.product.models.bindings import ProductModelBindingResolver
from mote.product.models.compiler import AdapterFactoryRevision, ProviderCatalogRevision
from mote.product.models.credential_sources import ProductCredentialSourceCatalog
from mote.product.models.generation_builder import (
    NewModelGeneration,
    ReusedModelGeneration,
    build_or_reuse_model_generation,
)
from mote.product.models.registry import LLMProviderRegistry
from mote.product.models.runtime_generation import build_model_runtime_generation
from mote.runtime.events import observe_event_sync
from mote.runtime.inference.cache import ExactCacheIdentity, MemoryExactInferenceCache
from mote.runtime.models.cached_gateway import ExactCachedModelGateway
from mote.runtime.models.composition import build_runtime_composition
from mote.runtime.models.cost import CostTracker
from mote.runtime.models.failover.snapshot import build_canonical_model_runtime_snapshot
from mote.runtime.models.model_gateway import RuntimeModelGateway
from mote.runtime.resilience.admission import ResourceAdmissionController


def _exact_cache_decorator(exact_cache, identity, cache):
    def decorate(gateway):
        return ExactCachedModelGateway(
            gateway,
            cache,
            identity=identity,
            ttl_seconds=exact_cache.default_ttl_seconds,
            sensitive_data_allowed=exact_cache.sensitive_data_allowed,
        )

    return decorate


def _provider_revision(providers: LLMProviderRegistry) -> ProviderCatalogRevision:
    entries = sorted(f"{key!s}:{value.__module__}.{value.__qualname__}" for key, value in providers.providers.items())
    return ProviderCatalogRevision(hashlib.sha256("\0".join(entries).encode()).hexdigest())


def _inference_revision(config) -> str:
    payload = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


async def build_application_candidate(
    config: Config,
    *,
    reload_sequence: ReloadSequence,
    source_revision: SourceRevision,
    providers: LLMProviderRegistry,
    oauth_root: Path,
    current=None,
    cost_tracker: CostTracker | None = None,
    admission_controller: ResourceAdmissionController | None = None,
    model_call_journal=None,
) -> ApplicationCompositionCandidate:
    credential_sources = ProductCredentialSourceCatalog(config.models, oauth_root=oauth_root)
    built = await build_or_reuse_model_generation(
        config.models,
        provider_catalog_revision=_provider_revision(providers),
        adapter_factory_revision=AdapterFactoryRevision("mote.product-model-adapter/v1"),
        credential_sources=credential_sources,
        current=current,
        inference_revision=_inference_revision(config.inference),
    )
    if isinstance(built, ReusedModelGeneration):
        handle = cast(SharedRuntimeCompositionHandle, built.handle)
    else:
        assert isinstance(built, NewModelGeneration)
        compiled = built.compiled
        handle = None
        try:
            snapshot = build_canonical_model_runtime_snapshot(compiled.topology)
            artifacts = ProductInferenceArtifacts(oauth_root.parent)
            generation = await build_model_runtime_generation(
                compiled,
                config.inference,
                state_root=oauth_root.parent,
                artifact_resolver=artifacts.resolve,
                artifact_publisher=artifacts.publish,
            )
            executor = RuntimeModelGateway(
                generation.planner,
                cost_tracker=cost_tracker,
                admission_controller=admission_controller,
                model_call_journal=model_call_journal,
            )
            gateway_decorator: Callable | None = None
            exact_cache = config.inference.cache.exact
            semantic_cache = config.inference.cache.semantic
            principal = generation.principal
            if semantic_cache.enabled:
                raise ValueError("Semantic cache backend and governed embedding executor are not configured")
            if exact_cache.enabled:
                if config.inference.deployment is DeploymentMode.SHARED_PROCESS:
                    raise ValueError("Shared Process exact cache requires a daemon-owned cache backend")
                if principal is None:
                    raise ValueError("Exact response cache requires a generation principal")
                cache = MemoryExactInferenceCache(maximum_entries=exact_cache.maximum_entries)
                identity = ExactCacheIdentity(
                    tenant_id=principal.tenant_id,
                    namespace=principal.project_id,
                    generation_revision=generation.revision,
                    policy_revision=principal.policy_revision,
                    model_capability_identity=generation.generation_artifact_digest,
                )

                gateway_decorator = _exact_cache_decorator(exact_cache, identity, cache)

            handle = build_runtime_composition(
                runtime_generation_id=RuntimeGenerationId(uuid4().hex),
                executor=executor,
                generation=generation,
                gateway_decorator=gateway_decorator,
                reuse_key=built.reuse_key,
                artifact_store=artifacts.store,
                artifact_reader=artifacts.resolve,
            )
            binding_resolver = ProductModelBindingResolver(compiled.credential_bindings)
            for endpoint in snapshot.endpoints:
                for slot_id in snapshot.slots_for_endpoint(endpoint.endpoint_id):
                    binding = binding_resolver.resolve(endpoint, slot_id)
                    if binding is None:
                        raise ValueError(f"endpoint {endpoint.endpoint_id!r} slot {slot_id!r} is not constructible")
        except BaseException as primary:
            cleanup_error = None
            if handle is not None:
                try:
                    await handle.release()
                except BaseException as exc:
                    cleanup_error = exc
            observe_event_sync(
                ApplicationReadinessFailed(
                    component_kind="model_generation",
                    error_code="MODEL_GENERATION_NOT_CONSTRUCTIBLE",
                )
            )
            if cleanup_error is not None:
                raise cleanup_error from primary
            raise
    return ApplicationCompositionCandidate(
        source_revision=source_revision,
        reload_sequence=reload_sequence,
        model=handle,
        runtime_role_config=RuntimeRoleConfigView(response_language=config.models.response_language),
        product_config=config.model_dump(mode="python", exclude={"models"}),
    )


__all__ = ["build_application_candidate"]
