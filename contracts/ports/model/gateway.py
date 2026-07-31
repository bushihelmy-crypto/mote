"""Provider-neutral model execution port consumed by Kernel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mote.contracts.model.failover import EndpointDescriptor
from mote.contracts.model.invocation import ModelInvocation, ResolvedModelResponse
from mote.contracts.model.topology import RouteId
from mote.contracts.ports.artifact.store import ArtifactResolver
from mote.contracts.ports.model.request_transformer import ModelRequestTransformer
from mote.contracts.ports.session.facts import SessionFactSink


@runtime_checkable
class ModelGateway(Protocol):
    def supports_route(self, route_id: RouteId) -> bool:
        ...

    def route_profile(self, route_id: RouteId) -> EndpointDescriptor | None:
        ...

    def route_profiles(self, route_id: RouteId) -> tuple[EndpointDescriptor, ...]:
        ...

    async def execute(
        self,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> ResolvedModelResponse:
        ...

    async def resume(
        self,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> ResolvedModelResponse:
        ...


@dataclass(frozen=True)
class ModelRoute:
    """One provider-neutral route and its request-scoped Runtime capabilities."""

    gateway: ModelGateway
    route_id: RouteId
    profile: EndpointDescriptor
    routing_decision_id: str | None = None
    request_transformer: ModelRequestTransformer | None = None
    session_fact_sink: SessionFactSink | None = None
    artifact_resolver: ArtifactResolver | None = None


__all__ = ["ModelGateway", "ModelRoute"]
