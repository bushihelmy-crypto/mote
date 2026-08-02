from __future__ import annotations

from mote.contracts.config.model.routing import AgentRouterConfig, RouterConfig, SemanticRouteConfig
from mote.contracts.model.failover import EndpointDescriptor, ResolvedEndpointCapabilities
from mote.contracts.model.topology import SemanticRoute
from mote.runtime.models.routing.catalog import build_route_catalog


class Gateway:
    def __init__(self) -> None:
        self.profile_requests: list[str] = []

    def supports_route(self, route_id: SemanticRoute) -> bool:
        return route_id.name in {"interactive.low", "interactive.strong", "summary"}

    def route_profile(self, route_id: SemanticRoute) -> EndpointDescriptor:
        self.profile_requests.append(route_id.name)
        return EndpointDescriptor(
            endpoint_id=f"{route_id.name}-endpoint",
            transport="test",
            provider="test",
            model=route_id.name,
            base_url_identity="test://models",
            capabilities=ResolvedEndpointCapabilities(context_tokens=100_000),
            credential_pool_id="test",
            lifecycle_revision="1",
        )

    def route_profiles(self, route_id: SemanticRoute) -> tuple[EndpointDescriptor, ...]:
        return (self.route_profile(route_id),)


def test_catalog_compiles_only_declared_semantic_routes() -> None:
    gateway = Gateway()
    router = RouterConfig(
        routes={
            "interactive.low": SemanticRouteConfig(quality_class="R0", quality_rank=0),
            "interactive.strong": SemanticRouteConfig(quality_class="R3", quality_rank=3),
        }
    )
    agent = AgentRouterConfig(
        strategy="rule",
        default_route="interactive.low",
        candidates=("interactive.low", "interactive.strong"),
    )

    catalog = build_route_catalog(router, agent, gateway)

    assert tuple(candidate.route_id for candidate in catalog.candidates) == (
        SemanticRoute(name="interactive.low"),
        SemanticRoute(name="interactive.strong"),
    )
    assert "summary" not in gateway.profile_requests
