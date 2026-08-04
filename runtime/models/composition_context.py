"""Task-local access to the Runtime composition lease of the active turn."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Protocol

from mote.contracts.model.failover import EndpointDescriptor
from mote.contracts.model.invocation import ModelInvocation, ResolvedModelResponse
from mote.contracts.model.topology import RouteId
from mote.contracts.ports.artifact.store import ArtifactResolver
from mote.contracts.ports.model.gateway import ModelGateway
from mote.contracts.ports.model.recovery import ModelRecoveryInspection
from mote.contracts.ports.model.request_transformer import ModelRequestTransformer
from mote.contracts.ports.session.facts import SessionFactSink


class RuntimeCompositionScopeError(RuntimeError):
    pass


class RuntimeCompositionLeaseView(Protocol):
    gateway: ModelGateway


_CURRENT_RUNTIME_COMPOSITION: ContextVar[RuntimeCompositionLeaseView | None] = ContextVar(
    "mote_runtime_composition", default=None
)


def bind_runtime_composition(lease: RuntimeCompositionLeaseView) -> Token[RuntimeCompositionLeaseView | None]:
    return _CURRENT_RUNTIME_COMPOSITION.set(lease)


def reset_runtime_composition(token: Token[RuntimeCompositionLeaseView | None]) -> None:
    _CURRENT_RUNTIME_COMPOSITION.reset(token)


def current_runtime_composition() -> RuntimeCompositionLeaseView:
    lease = _CURRENT_RUNTIME_COMPOSITION.get()
    if lease is None:
        raise RuntimeCompositionScopeError("model access requires an active application turn lease")
    return lease


class CurrentRuntimeModelGateway:
    """Gateway proxy that borrows the active turn lease without acquiring."""

    @staticmethod
    def _gateway() -> ModelGateway:
        return current_runtime_composition().gateway

    def supports_route(self, route_id: RouteId) -> bool:
        return self._gateway().supports_route(route_id)

    def route_profile(self, route_id: RouteId) -> EndpointDescriptor | None:
        return self._gateway().route_profile(route_id)

    def route_profiles(self, route_id: RouteId) -> tuple[EndpointDescriptor, ...]:
        return self._gateway().route_profiles(route_id)

    def inspect_recovery(self, model_call_id: str) -> ModelRecoveryInspection:
        return self._gateway().inspect_recovery(model_call_id)

    async def execute(
        self,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> ResolvedModelResponse:
        return await self._gateway().execute(
            invocation,
            request_transformer=request_transformer,
            stream=stream,
            session_fact_sink=session_fact_sink,
            artifact_resolver=artifact_resolver,
        )

    async def resume(
        self,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> ResolvedModelResponse:
        return await self._gateway().resume(
            invocation,
            request_transformer=request_transformer,
            stream=stream,
            session_fact_sink=session_fact_sink,
            artifact_resolver=artifact_resolver,
        )


__all__ = [
    "CurrentRuntimeModelGateway",
    "RuntimeCompositionScopeError",
    "bind_runtime_composition",
    "current_runtime_composition",
    "reset_runtime_composition",
]
