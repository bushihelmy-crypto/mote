"""Provider-neutral model execution port consumed by Kernel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mote.contracts.models.failover import EndpointDescriptor
from mote.contracts.models.invocation import ModelInvocation, ResolvedModelResponse
from mote.contracts.ports.artifact_store import ArtifactResolver
from mote.contracts.ports.model_request_transformer import ModelRequestTransformer
from mote.contracts.ports.session_facts import SessionFactSink


@runtime_checkable
class ModelGateway(Protocol):
    def supports_route(self, route_id: str) -> bool:
        ...

    def route_profile(self, route_id: str) -> EndpointDescriptor | None:
        ...

    def route_profiles(self, route_id: str) -> tuple[EndpointDescriptor, ...]:
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
    route_id: str
    profile: EndpointDescriptor
    routing_decision_id: str | None = None
    request_transformer: ModelRequestTransformer | None = None
    session_fact_sink: SessionFactSink | None = None
    artifact_resolver: ArtifactResolver | None = None


__all__ = ["ModelGateway", "ModelRoute"]
