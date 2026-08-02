"""Web-search backends projected onto Runtime's hosted-service lifecycle."""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from mote.contracts.model import WebSearchHit
from mote.contracts.model.failover import (
    AttemptBudget,
    EndpointDescriptor,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    Retryability,
)
from mote.contracts.model.invocation import WebSearchHitOutput
from mote.contracts.model.topology import TaskRoute
from mote.contracts.ports.model.gateway import ModelGateway, ModelRoute
from mote.contracts.service import (
    ServiceAcceptance,
    ServiceCompleted,
    ServiceEndpointDescriptor,
    ServiceEndpointFailure,
    ServiceEndpointOutcome,
    ServiceInvocation,
    ServiceReceipt,
    ServiceResponse,
    WebSearchPayload,
    WebSearchResult,
)
from mote.contracts.tool.errors import ToolNotConfiguredError
from mote.product.config.web_search import WebSearchConfig
from mote.product.web_search.registry import SearchBackendRegistry
from mote.runtime.models.model_calls import web_search as search_with_model
from mote.runtime.resilience.admission import AdmissionRejectedError
from mote.runtime.resilience.error_classification import classify_llm_error
from mote.runtime.resilience.failover.classification import classify_failure
from mote.runtime.service_gateway.snapshot import ServiceFailoverGroup, ServiceRuntimeSnapshot

_CAPABILITY = "web.search"
_ROUTE = "web.search"
_MODEL_ROUTE = TaskRoute(name="web_search")


class WebSearchServiceEndpointAdapter:
    """Execute one search backend request without owning retry or routing state."""

    def __init__(
        self,
        *,
        endpoint_id: str,
        credential_slot_id: str,
        tenant_fingerprint: str,
        config: WebSearchConfig,
        backends: SearchBackendRegistry,
        model_gateway: ModelGateway | None,
    ) -> None:
        self.endpoint_id = endpoint_id
        self.credential_slot_id = credential_slot_id
        self.tenant_fingerprint = tenant_fingerprint
        self._config = config
        self._backends = backends
        self._model_gateway = model_gateway

    async def start_once(
        self,
        invocation: ServiceInvocation,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> ServiceEndpointOutcome:
        del timeout_seconds
        self._validate_binding(invocation, endpoint)
        payload = invocation.payload
        if not isinstance(payload, WebSearchPayload):
            raise ValueError("web-search endpoint requires a WebSearchPayload")
        query = payload.query
        allowed_domains = list(payload.allowed_domains)
        blocked_domains = list(payload.blocked_domains)

        async def provider_search(
            query: str,
            *,
            allowed_domains: list[str] | None = None,
            blocked_domains: list[str] | None = None,
        ) -> list[WebSearchHit]:
            return await self._provider_search(
                invocation,
                query,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
            )

        backend = self._backends.create(
            self._config,
            provider_search=provider_search,
        )
        hits = await backend.search(
            query,
            allowed_domains=allowed_domains or None,
            blocked_domains=blocked_domains or None,
        )
        return ServiceCompleted(
            response=ServiceResponse(
                value=WebSearchResult(
                    hits=tuple(
                        WebSearchHitOutput(
                            title=hit.title,
                            url=hit.url,
                            snippet=hit.snippet,
                        )
                        for hit in hits
                    )
                )
            )
        )

    async def poll_once(
        self,
        receipt: ServiceReceipt,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> ServiceEndpointOutcome:
        del receipt, endpoint, timeout_seconds
        raise RuntimeError("web search is a one-shot service and cannot be polled")

    async def reconcile_once(
        self,
        invocation: ServiceInvocation,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> ServiceEndpointOutcome | None:
        return await self.start_once(
            invocation,
            endpoint,
            timeout_seconds=timeout_seconds,
        )

    async def cancel_once(
        self,
        receipt: ServiceReceipt,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> None:
        del receipt, endpoint, timeout_seconds

    def classify_start(self, exc: Exception) -> ServiceEndpointFailure:
        if isinstance(exc, AdmissionRejectedError):
            return ServiceEndpointFailure(
                disposition=exc.disposition,
                acceptance=ServiceAcceptance.REJECTED,
            )
        if isinstance(exc, (NotImplementedError, ToolNotConfiguredError, ValueError)):
            return ServiceEndpointFailure(
                disposition=FailureDisposition(
                    reason=FailureReason.PROTOCOL_INCOMPATIBLE,
                    domain=FailureDomain.PROTOCOL,
                    retryability=Retryability.NEVER,
                    health_verdict=HealthVerdict.NEUTRAL,
                ),
                acceptance=ServiceAcceptance.REJECTED,
            )
        translated = classify_llm_error(exc) or exc
        return ServiceEndpointFailure(
            disposition=classify_failure(translated),
            acceptance=ServiceAcceptance.UNKNOWN,
        )

    def classify_poll(self, exc: Exception) -> ServiceEndpointFailure:
        return self.classify_start(exc)

    async def aclose(self) -> None:
        return None

    async def _provider_search(
        self,
        invocation: ServiceInvocation,
        query: str,
        *,
        allowed_domains: list[str] | None,
        blocked_domains: list[str] | None,
    ) -> list[WebSearchHit]:
        gateway = self._model_gateway
        if gateway is None or not gateway.supports_route(_MODEL_ROUTE):
            raise NotImplementedError("the web-search model route is unavailable")
        profile = gateway.route_profile(_MODEL_ROUTE)
        if profile is None:
            raise NotImplementedError("the web-search model route has no profile")
        output = await search_with_model(
            ModelRoute(gateway=gateway, route_id=_MODEL_ROUTE, profile=profile),
            query,
            model_call_id=_model_call_id(invocation.service_call_id),
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            max_uses=(invocation.payload.max_uses if isinstance(invocation.payload, WebSearchPayload) else 8),
            trace_id=invocation.trace_id,
        )
        return [WebSearchHit(title=hit.title, url=hit.url, snippet=hit.snippet) for hit in output.hits]

    def _validate_binding(
        self,
        invocation: ServiceInvocation,
        endpoint: ServiceEndpointDescriptor,
    ) -> None:
        if endpoint.endpoint_id != self.endpoint_id:
            raise ValueError("web-search adapter endpoint binding does not match")
        if invocation.capability != _CAPABILITY or endpoint.capability != _CAPABILITY:
            raise ValueError("web-search adapter received an unsupported capability")


class WebSearchServiceEndpointResolver:
    """Resolve the configured search backend into its single-wire adapter."""

    def __init__(
        self,
        config: WebSearchConfig,
        backends: SearchBackendRegistry,
        model_gateway: ModelGateway | None,
    ) -> None:
        self._config = config
        self._backends = backends
        self._model_gateway = model_gateway

    def resolve(
        self,
        endpoint: ServiceEndpointDescriptor,
        credential_slot_id: str,
    ) -> WebSearchServiceEndpointAdapter | None:
        if endpoint.capability != _CAPABILITY:
            return None
        expected_slot = _credential_slot_id(self._config)
        if credential_slot_id != expected_slot:
            return None
        return WebSearchServiceEndpointAdapter(
            endpoint_id=endpoint.endpoint_id,
            credential_slot_id=credential_slot_id,
            tenant_fingerprint=_tenant_fingerprint(self._config),
            config=self._config,
            backends=self._backends,
            model_gateway=self._model_gateway,
        )

    async def aclose(self) -> None:
        return None


def build_web_search_service_snapshot(
    config: WebSearchConfig,
    model_gateway: ModelGateway | None,
) -> ServiceRuntimeSnapshot:
    backend = config.backend or "provider"
    profile = (
        model_gateway.route_profile(_MODEL_ROUTE)
        if backend == "provider" and model_gateway is not None and model_gateway.supports_route(_MODEL_ROUTE)
        else None
    )
    revision = _config_revision(config, profile)
    endpoint_id = f"web-search.{backend}"
    endpoint = ServiceEndpointDescriptor(
        endpoint_id=endpoint_id,
        capability=_CAPABILITY,
        transport=("model_gateway" if backend == "provider" else urlparse(config.base_url).scheme or "https"),
        provider=backend,
        base_url_identity=_fingerprint(
            profile.base_url_identity if profile is not None else config.base_url or f"backend:{backend}"
        ),
        credential_pool_id=f"{endpoint_id}.credentials",
        lifecycle_revision=revision,
    )
    return ServiceRuntimeSnapshot(
        revision=revision,
        endpoints=(endpoint,),
        groups=(
            ServiceFailoverGroup(
                group_id=_ROUTE,
                endpoint_ids=(endpoint_id,),
                budget=AttemptBudget(
                    max_wire_attempts=3,
                    max_attempts_per_endpoint=3,
                    max_endpoint_switches=0,
                    max_credential_rotations=0,
                    max_request_transforms=0,
                    total_deadline_seconds=900.0,
                    single_attempt_timeout_seconds=300.0,
                    max_backoff_seconds=30.0,
                ),
            ),
        ),
        route_groups=((_ROUTE, _ROUTE),),
        credential_slots=((endpoint_id, (_credential_slot_id(config),)),),
    )


def _model_call_id(service_call_id: str) -> str:
    return hashlib.sha256(f"web-search-model\0{service_call_id}".encode("utf-8")).hexdigest()


def _credential_slot_id(config: WebSearchConfig) -> str:
    return f"web-search.{config.backend or 'provider'}.credential.{_tenant_fingerprint(config)}"


def _tenant_fingerprint(config: WebSearchConfig) -> str:
    if (config.backend or "provider") == "provider":
        return _fingerprint("model-route:web_search")
    return _fingerprint(config.api_key or f"backend:{config.backend}")


def _config_revision(config: WebSearchConfig, profile: EndpointDescriptor | None) -> str:
    profile_revision = profile.lifecycle_revision if profile is not None else ""
    return _fingerprint(
        "\0".join(
            (
                config.backend or "provider",
                config.base_url,
                str(profile_revision),
            )
        )
    )


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


__all__ = [
    "WebSearchServiceEndpointResolver",
    "build_web_search_service_snapshot",
]
