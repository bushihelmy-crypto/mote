"""Immutable semantic-route catalog compilation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from mote.contracts.config.model.routing import AgentRouterConfig, RouterConfig
from mote.contracts.model.routing import RouteAdmissionProfile, RouteCandidate, RouteCapabilities
from mote.contracts.model.topology import SemanticRoute
from mote.contracts.model.topology_codec import encode_route_id
from mote.contracts.ports.model.gateway import ModelGateway


@dataclass(frozen=True)
class RouteCatalogSnapshot:
    revision: str
    candidates: tuple[RouteCandidate, ...]
    default_route_id: SemanticRoute
    class_routes: tuple[tuple[str, SemanticRoute], ...]

    def candidate(self, route_id: SemanticRoute) -> RouteCandidate | None:
        return next(
            (candidate for candidate in self.candidates if candidate.route_id == route_id),
            None,
        )

    def route_for_class(self, route_class: str) -> SemanticRoute | None:
        return next(
            (route_id for name, route_id in self.class_routes if name == route_class),
            None,
        )


def build_route_catalog(
    router: RouterConfig,
    agent: AgentRouterConfig,
    gateway: ModelGateway,
) -> RouteCatalogSnapshot:
    """Pin one validated, secret-free policy view of configured routes."""

    route_ids = agent.candidates or tuple(router.routes)
    candidates: list[RouteCandidate] = []
    for route_name in route_ids:
        route_id = SemanticRoute(name=route_name)
        metadata = router.routes[route_name]
        if not metadata.enabled or not gateway.supports_route(route_id):
            continue
        profiles = gateway.route_profiles(route_id)
        if not profiles:
            continue
        profile = profiles[0]
        route_capabilities = RouteCapabilities(
            supports_tools=any(item.capabilities.supports_tools for item in profiles),
            supports_native_schema=any(item.capabilities.supports_native_schema for item in profiles),
            supports_server_web_search=any(item.capabilities.supports_server_web_search for item in profiles),
            supports_vision=any(item.capabilities.supports_vision for item in profiles),
            supports_pdf=any(item.capabilities.supports_pdf for item in profiles),
            supports_native_tool_search=any(item.capabilities.supports_native_tool_search for item in profiles),
        )
        candidates.append(
            RouteCandidate(
                route_id=route_id,
                quality_class=metadata.quality_class,
                quality_rank=metadata.quality_rank,
                cost_class=metadata.cost_class,
                latency_class=metadata.latency_class,
                context_tokens=max(item.capabilities.context_tokens for item in profiles),
                capabilities=route_capabilities,
                admission_profiles=tuple(
                    RouteAdmissionProfile(
                        context_tokens=item.capabilities.context_tokens,
                        capabilities=RouteCapabilities(
                            supports_tools=item.capabilities.supports_tools,
                            supports_native_schema=item.capabilities.supports_native_schema,
                            supports_server_web_search=item.capabilities.supports_server_web_search,
                            supports_vision=item.capabilities.supports_vision,
                            supports_pdf=item.capabilities.supports_pdf,
                            supports_native_tool_search=item.capabilities.supports_native_tool_search,
                        ),
                        governance_domain=item.governance_domain,
                        region=item.region,
                    )
                    for item in profiles
                ),
                governance_domain=profile.governance_domain,
                allowed_regions=frozenset(item.region for item in profiles),
                data_classifications=metadata.data_classifications,
                tags=metadata.tags,
                enabled=True,
            )
        )

    public_shape = {
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        "default": encode_route_id(SemanticRoute(name=agent.default_route)),
        "class_routes": sorted(agent.class_routes.items()),
    }
    revision = hashlib.sha256(json.dumps(public_shape, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return RouteCatalogSnapshot(
        revision=revision,
        candidates=tuple(candidates),
        default_route_id=SemanticRoute(name=agent.default_route),
        class_routes=tuple(
            (name, SemanticRoute(name=route_id)) for name, route_id in sorted(agent.class_routes.items())
        ),
    )


__all__ = ["RouteCatalogSnapshot", "build_route_catalog"]
