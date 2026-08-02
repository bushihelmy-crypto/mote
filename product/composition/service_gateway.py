"""Composition of Product-owned hosted-service capability families."""

from __future__ import annotations

from mote.contracts.config.model.breaker import BreakerConfig
from mote.contracts.ports.model.gateway import ModelGateway
from mote.contracts.ports.service.call_journal import ServiceCallJournal
from mote.contracts.ports.service.gateway import ServiceGateway
from mote.product.composition.service_endpoints import ProductServiceEndpointResolver
from mote.product.config.multimodal import MultimodalConfig
from mote.product.config.web_search import WebSearchConfig
from mote.product.media_generation.catalog import builtin_media_provider_registry
from mote.product.media_generation.registry import MediaProviderRegistry
from mote.product.media_generation.service import MediaServiceEndpointResolver, build_media_service_snapshot
from mote.product.web_search.registry import SearchBackendRegistry, builtin_search_backend_registry
from mote.product.web_search.service import WebSearchServiceEndpointResolver, build_web_search_service_snapshot
from mote.runtime.resilience.admission import ResourceAdmissionController
from mote.runtime.service_gateway import RuntimeServiceGateway, ServiceFailoverPlanner, merge_service_runtime_snapshots


def builtin_service_gateway(
    multimodal: MultimodalConfig,
    web_search: WebSearchConfig,
    *,
    model_gateway: ModelGateway | None,
    model_profile_gateway: ModelGateway | None = None,
    media_providers: MediaProviderRegistry | None = None,
    search_backends: SearchBackendRegistry | None = None,
    breaker_config: BreakerConfig | None = None,
    admission_controller: ResourceAdmissionController | None = None,
    service_call_journal: ServiceCallJournal | None = None,
    activate_reconciliation: bool = False,
) -> ServiceGateway:
    if admission_controller is not None and breaker_config is not None:
        raise ValueError("admission_controller already owns its breaker configuration")
    providers = media_providers or builtin_media_provider_registry()
    backends = search_backends or builtin_search_backend_registry()
    snapshot = merge_service_runtime_snapshots(
        build_media_service_snapshot(multimodal),
        build_web_search_service_snapshot(web_search, model_profile_gateway or model_gateway),
    )
    gateway = RuntimeServiceGateway(
        ServiceFailoverPlanner(snapshot),
        ProductServiceEndpointResolver(
            MediaServiceEndpointResolver(multimodal, providers),
            WebSearchServiceEndpointResolver(web_search, backends, model_gateway),
        ),
        admission_controller=(admission_controller or ResourceAdmissionController(breaker_config=breaker_config)),
        service_call_journal=service_call_journal,
    )
    if activate_reconciliation:
        gateway.activate_reconciliation()
    return gateway


__all__ = ["builtin_service_gateway"]
