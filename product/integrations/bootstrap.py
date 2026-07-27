"""Explicit Product integration bootstrap."""

from __future__ import annotations

from typing import Mapping

from mote.contracts.config.llm import LLMType
from mote.contracts.config.models import ModelsConfig
from mote.contracts.config.multimodal import MultimodalConfig
from mote.contracts.ports.model_call_journal import ModelCallJournal
from mote.contracts.ports.model_gateway import ModelGateway
from mote.contracts.ports.service_call_journal import ServiceCallJournal
from mote.contracts.ports.service_gateway import ServiceGateway
from mote.contracts.resilience import BreakerConfig
from mote.contracts.settings.web_search import WebSearchConfig
from mote.product.integrations.models import (
    AnthropicLLM,
    DeepSeekLLM,
    OpenAILLM,
    OpenAIResponsesLLM,
    ProductModelEndpointResolver,
)
from mote.product.integrations.services import (
    MediaServiceEndpointResolver,
    ProductServiceEndpointResolver,
    WebSearchServiceEndpointResolver,
    build_media_service_snapshot,
    build_web_search_service_snapshot,
)
from mote.product.toolsets.builtin.generate_media.bootstrap import builtin_media_provider_registry
from mote.product.toolsets.builtin.generate_media.registry import MediaProviderRegistry
from mote.product.toolsets.builtin.web_search_registry import SearchBackendRegistry, builtin_search_backend_registry
from mote.runtime.models.clients.registry import LLMProviderRegistry
from mote.runtime.models.cost import CostTracker
from mote.runtime.models.failover.admission import ResourceAdmissionController
from mote.runtime.models.failover.planner import FailoverPlanner
from mote.runtime.models.failover.snapshot import build_model_runtime_snapshot
from mote.runtime.models.model_gateway import RuntimeModelGateway
from mote.runtime.service_gateway import RuntimeServiceGateway, ServiceFailoverPlanner, merge_service_runtime_snapshots


def builtin_provider_registry() -> LLMProviderRegistry:
    """Build an isolated catalog containing the Product's bundled providers."""
    registry = LLMProviderRegistry()
    for key in (
        LLMType.OPENAI,
        LLMType.FIREWORKS,
        LLMType.OPEN_LLM,
        LLMType.MOONSHOT,
        LLMType.MISTRAL,
        LLMType.YI,
        LLMType.OPEN_ROUTER,
        LLMType.SILICONFLOW,
    ):
        registry.register(key, OpenAILLM)
    registry.register(LLMType.OPENAI_RESPONSES, OpenAIResponsesLLM)
    registry.register(LLMType.ANTHROPIC, AnthropicLLM)
    registry.register(LLMType.DEEPSEEK, DeepSeekLLM)
    return registry


def builtin_model_gateway(
    models: ModelsConfig,
    *,
    providers: LLMProviderRegistry | None = None,
    environ: Mapping[str, str] | None = None,
    cost_tracker: CostTracker | None = None,
    breaker_config: BreakerConfig | None = None,
    admission_controller: ResourceAdmissionController | None = None,
    model_call_journal: ModelCallJournal | None = None,
) -> ModelGateway:
    """Compose the canonical Runtime gateway with Product provider adapters."""

    if admission_controller is not None and breaker_config is not None:
        raise ValueError("admission_controller already owns its breaker configuration")
    catalog = providers or builtin_provider_registry()
    snapshot = build_model_runtime_snapshot(models)
    return RuntimeModelGateway(
        FailoverPlanner(snapshot),
        ProductModelEndpointResolver(models, catalog, environ=environ),
        cost_tracker=cost_tracker,
        admission_controller=(admission_controller or ResourceAdmissionController(breaker_config=breaker_config)),
        model_call_journal=model_call_journal,
    )


def builtin_service_gateway(
    multimodal: MultimodalConfig,
    web_search: WebSearchConfig,
    *,
    model_gateway: ModelGateway | None,
    media_providers: MediaProviderRegistry | None = None,
    search_backends: SearchBackendRegistry | None = None,
    breaker_config: BreakerConfig | None = None,
    admission_controller: ResourceAdmissionController | None = None,
    service_call_journal: ServiceCallJournal | None = None,
) -> ServiceGateway:
    """Compose the hosted-Tool gateway from Product-owned service families."""

    if admission_controller is not None and breaker_config is not None:
        raise ValueError("admission_controller already owns its breaker configuration")
    providers = media_providers or builtin_media_provider_registry()
    backends = search_backends or builtin_search_backend_registry()
    snapshot = merge_service_runtime_snapshots(
        build_media_service_snapshot(multimodal),
        build_web_search_service_snapshot(web_search, model_gateway),
    )
    return RuntimeServiceGateway(
        ServiceFailoverPlanner(snapshot),
        ProductServiceEndpointResolver(
            MediaServiceEndpointResolver(multimodal, providers),
            WebSearchServiceEndpointResolver(
                web_search,
                backends,
                model_gateway,
            ),
        ),
        admission_controller=(admission_controller or ResourceAdmissionController(breaker_config=breaker_config)),
        service_call_journal=service_call_journal,
    )


async def reload_builtin_model_gateway(
    gateway: RuntimeModelGateway,
    models: ModelsConfig,
    *,
    providers: LLMProviderRegistry | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Atomically activate a newly validated Product model generation."""

    catalog = providers or builtin_provider_registry()
    snapshot = build_model_runtime_snapshot(models)
    return await gateway.reload(
        FailoverPlanner(snapshot),
        ProductModelEndpointResolver(models, catalog, environ=environ),
    )


__all__ = [
    "builtin_model_gateway",
    "builtin_provider_registry",
    "builtin_service_gateway",
    "reload_builtin_model_gateway",
]
