from __future__ import annotations

import pytest

from mote.contracts.models import WebSearchHit
from mote.contracts.models.failover import EndpointCapabilities, EndpointDescriptor
from mote.contracts.models.invocation import ResolvedModelResponse, WebSearchHitOutput, WebSearchOutput
from mote.contracts.services import ServiceExecutionSemantics, ServiceInvocation
from mote.contracts.settings.web_search import WebSearchConfig
from mote.product.integrations.services.web_search import (
    WebSearchServiceEndpointResolver,
    build_web_search_service_snapshot,
)
from mote.product.toolsets.builtin.web_search_registry import SearchBackend, builtin_search_backend_registry
from mote.runtime.service_gateway import LocalServiceCallJournal, RuntimeServiceGateway, ServiceFailoverPlanner


class _ModelGateway:
    def __init__(self) -> None:
        self.model_call_ids: list[str] = []
        self.profile = EndpointDescriptor(
            endpoint_id="model-search",
            transport="anthropic",
            provider="anthropic",
            model="search-model",
            base_url_identity="https://model.example",
            capabilities=EndpointCapabilities(supports_server_web_search=True),
            credential_pool_id="model-credentials",
            lifecycle_revision="model-revision",
        )

    def supports_route(self, route_id: str) -> bool:
        return route_id == "web_search"

    def route_profile(self, route_id: str):
        return self.profile if self.supports_route(route_id) else None

    def route_profiles(self, route_id: str):
        profile = self.route_profile(route_id)
        return (profile,) if profile is not None else ()

    async def execute(self, invocation, **kwargs):
        del kwargs
        self.model_call_ids.append(invocation.model_call_id)
        return ResolvedModelResponse(
            output=WebSearchOutput(
                hits=(
                    WebSearchHitOutput(
                        title="Result",
                        url="https://example.com/result",
                        snippet="snippet",
                    ),
                )
            ),
            endpoint_id="model-search",
            endpoint_fingerprint="model-fingerprint",
            model_or_deployment="search-model",
            tenant_fingerprint="tenant",
            credential_slot_id="model-slot",
            model_call_id=invocation.model_call_id,
            successful_attempt_id="model-attempt",
        )

    async def resume(self, invocation, **kwargs):
        return await self.execute(invocation, **kwargs)


def _invocation(service_call_id: str = "service-call") -> ServiceInvocation:
    return ServiceInvocation(
        service_call_id=service_call_id,
        route_id="web.search",
        capability="web.search",
        payload={
            "query": "python",
            "allowed_domains": ["python.org"],
            "blocked_domains": [],
            "max_uses": 8,
        },
        semantics=ServiceExecutionSemantics.PURE,
        idempotency_key=f"key-{service_call_id}",
    )


@pytest.mark.asyncio
async def test_provider_backend_uses_stable_model_logical_call() -> None:
    model_gateway = _ModelGateway()
    config = WebSearchConfig()
    snapshot = build_web_search_service_snapshot(config, model_gateway)
    resolver = WebSearchServiceEndpointResolver(
        config,
        builtin_search_backend_registry(),
        model_gateway,
    )
    endpoint = snapshot.endpoints[0]
    slot = snapshot.credential_slots[0][1][0]
    adapter = resolver.resolve(endpoint, slot)
    assert adapter is not None

    first = await adapter.start_once(_invocation(), endpoint, timeout_seconds=30)
    second = await adapter.reconcile_once(_invocation(), endpoint, timeout_seconds=30)

    assert first.kind == "completed"
    assert second is not None and second.kind == "completed"
    assert len(model_gateway.model_call_ids) == 2
    assert model_gateway.model_call_ids[0] == model_gateway.model_call_ids[1]
    assert first.response.value["hits"][0]["url"] == "https://example.com/result"


@pytest.mark.asyncio
async def test_direct_backend_retries_only_failed_pure_wire(tmp_path) -> None:
    class FlakySearch(SearchBackend):
        name = "flaky"
        calls = 0

        async def search(self, query, *, allowed_domains=None, blocked_domains=None):
            type(self).calls += 1
            if type(self).calls == 1:
                raise ConnectionError("temporary search outage")
            return [WebSearchHit(title="Recovered", url="https://example.com")]

    config = WebSearchConfig(
        backend="flaky",
        api_key="search-key",  # pragma: allowlist secret
        base_url="https://search.example",
    )
    backends = builtin_search_backend_registry()
    backends.register(FlakySearch.name, FlakySearch)
    snapshot = build_web_search_service_snapshot(config, None)
    gateway = RuntimeServiceGateway(
        ServiceFailoverPlanner(snapshot),
        WebSearchServiceEndpointResolver(config, backends, None),
        service_call_journal=LocalServiceCallJournal(tmp_path),
    )

    resolved = await gateway.execute(_invocation("direct-search"))

    assert FlakySearch.calls == 2
    assert resolved.response.value["hits"] == [{"title": "Recovered", "url": "https://example.com", "snippet": ""}]


def test_snapshot_is_a_secret_free_web_search_route() -> None:
    config = WebSearchConfig(
        backend="vendor",
        api_key="private-key",  # pragma: allowlist secret
        base_url="https://search.example/v1",
    )
    snapshot = build_web_search_service_snapshot(config, None)

    assert snapshot.group_for_route("web.search") is not None
    assert snapshot.endpoints[0].capability == "web.search"
    assert "private-key" not in repr(snapshot)
