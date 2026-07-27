from __future__ import annotations

from mote.contracts.config.routing import AgentRouterConfig, RouterConfig, SemanticRouteConfig
from mote.contracts.models.failover import EndpointCapabilities, EndpointDescriptor
from mote.runtime.models.routing.catalog import build_route_catalog


class Gateway:
    def __init__(self) -> None:
        self.profile_requests: list[str] = []

    def supports_route(self, route_id: str) -> bool:
        return route_id in {"interactive.low", "interactive.strong", "summary"}

    def route_profile(self, route_id: str) -> EndpointDescriptor:
        self.profile_requests.append(route_id)
        return EndpointDescriptor(
            endpoint_id=f"{route_id}-endpoint",
            transport="test",
            provider="test",
            model=route_id,
            base_url_identity="test://models",
            capabilities=EndpointCapabilities(context_tokens=100_000),
            credential_pool_id="test",
            lifecycle_revision="1",
        )

    def route_profiles(self, route_id: str) -> tuple[EndpointDescriptor, ...]:
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
        "interactive.low",
        "interactive.strong",
    )
    assert "summary" not in gateway.profile_requests
