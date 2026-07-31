"""Immutable Runtime model composition and reference-counted ownership."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from mote.contracts.model.failover import EndpointDescriptor
from mote.contracts.model.topology import DefaultRoute, RouteId
from mote.contracts.ports.artifact.provider_transfer import ProviderArtifactTransferRuntime
from mote.contracts.ports.artifact.store import ArtifactLookupIndex
from mote.contracts.ports.inference.session_runtime import SessionRuntime
from mote.contracts.ports.inference.wire_permit import WirePermitIssuer
from mote.contracts.ports.model.gateway import ModelGateway
from mote.contracts.ports.service.command_runtime import ServiceCommandRuntime
from mote.contracts.runtime.application import RuntimeGenerationId
from mote.runtime.models.failover.runtime_state import ModelRuntimeGeneration
from mote.runtime.models.model_gateway import GenerationBoundRuntimeModelGateway, RuntimeModelGateway


class LeaseReleasedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DefaultModelMetadata:
    model: str
    provider: str
    transport: str
    context_tokens: int


class ModelRoutePolicy:
    def __init__(self, generation: ModelRuntimeGeneration) -> None:
        self._generation = generation

    def supports(self, route_id: RouteId) -> bool:
        return self._generation.planner.snapshot.group_for_route(route_id) is not None

    def profile(self, route_id: RouteId) -> EndpointDescriptor | None:
        snapshot = self._generation.planner.snapshot
        group = snapshot.group_for_route(route_id)
        if group is None or not group.endpoint_ids:
            return None
        return snapshot.endpoint(group.endpoint_ids[0])


@dataclass(frozen=True, slots=True)
class RuntimeCompositionGeneration:
    runtime_generation_id: RuntimeGenerationId
    topology_revision: str
    gateway: ModelGateway
    route_policy: ModelRoutePolicy
    default_model: DefaultModelMetadata
    command_runtime: ServiceCommandRuntime | None
    session_runtime: SessionRuntime | None
    transfer_runtime: ProviderArtifactTransferRuntime | None
    permit_issuer: WirePermitIssuer | None
    permit_audience: str
    generation_id: str
    generation_artifact_digest: str
    artifact_store: ArtifactLookupIndex | None
    artifact_reader: Any
    _runtime_generation: ModelRuntimeGeneration


class RuntimeCompositionLease:
    __slots__ = ("_handle", "_generation", "_released")

    def __init__(
        self,
        handle: "SharedRuntimeCompositionHandle",
        generation: RuntimeCompositionGeneration,
    ) -> None:
        self._handle = handle
        self._generation = generation
        self._released = False

    def _live(self) -> RuntimeCompositionGeneration:
        if self._released:
            raise LeaseReleasedError("Runtime composition lease was released")
        return self._generation

    @property
    def runtime_generation_id(self) -> RuntimeGenerationId:
        return self._live().runtime_generation_id

    @property
    def topology_revision(self) -> str:
        return self._live().topology_revision

    @property
    def gateway(self) -> ModelGateway:
        return self._live().gateway

    @property
    def route_policy(self) -> ModelRoutePolicy:
        return self._live().route_policy

    @property
    def default_model(self) -> DefaultModelMetadata:
        return self._live().default_model

    @property
    def command_runtime(self) -> ServiceCommandRuntime | None:
        return self._live().command_runtime

    @property
    def session_runtime(self) -> SessionRuntime | None:
        return self._live().session_runtime

    @property
    def transfer_runtime(self) -> ProviderArtifactTransferRuntime | None:
        return self._live().transfer_runtime

    @property
    def permit_issuer(self) -> WirePermitIssuer | None:
        return self._live().permit_issuer

    @property
    def permit_audience(self) -> str:
        return self._live().permit_audience

    @property
    def generation_id(self) -> str:
        return self._live().generation_id

    @property
    def generation_artifact_digest(self) -> str:
        return self._live().generation_artifact_digest

    @property
    def artifact_store(self) -> ArtifactLookupIndex | None:
        return self._live().artifact_store

    @property
    def artifact_reader(self):
        return self._live().artifact_reader

    async def __aenter__(self) -> "RuntimeCompositionLease":
        self._live()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._released:
            return
        self._released = True
        await self._handle._release_lease()


class SharedRuntimeCompositionHandle:
    """One root reference plus any retained application/child references."""

    def __init__(self, generation: RuntimeCompositionGeneration, *, reuse_key: Any = None) -> None:
        self._generation = generation
        self._reuse_key = reuse_key
        self._references = 1
        self._leases = 0
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def runtime_generation_id(self) -> RuntimeGenerationId:
        return self._generation.runtime_generation_id

    @property
    def topology_revision(self) -> str:
        return self._generation.topology_revision

    @property
    def reuse_key(self):
        return self._reuse_key

    def retain(self) -> "SharedRuntimeCompositionHandle":
        if self._closed:
            raise LeaseReleasedError("Runtime composition handle is closed")
        self._references += 1
        return self

    async def acquire(self) -> RuntimeCompositionLease:
        async with self._lock:
            if self._closed or self._references == 0:
                raise LeaseReleasedError("Runtime composition handle is closed")
            self._leases += 1
            return RuntimeCompositionLease(self, self._generation)

    async def release(self) -> None:
        close = False
        async with self._lock:
            if self._references == 0:
                return
            self._references -= 1
            close = self._references == 0 and self._leases == 0
            if close:
                self._closed = True
        if close:
            await self._close_generation()

    async def _release_lease(self) -> None:
        close = False
        async with self._lock:
            if self._leases > 0:
                self._leases -= 1
            close = self._references == 0 and self._leases == 0 and not self._closed
            if close:
                self._closed = True
        if close:
            await self._close_generation()

    async def _close_generation(self) -> None:
        resources = self._generation._runtime_generation.closeables
        seen: set[int] = set()
        errors: list[BaseException] = []
        for resource in resources:
            if id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("Runtime model generation close failed", errors)


def build_runtime_composition(
    *,
    runtime_generation_id: RuntimeGenerationId,
    executor: RuntimeModelGateway,
    generation: ModelRuntimeGeneration,
    gateway_decorator=None,
    reuse_key: Any = None,
    artifact_store: ArtifactLookupIndex | None = None,
    artifact_reader=None,
) -> SharedRuntimeCompositionHandle:
    snapshot = generation.planner.snapshot
    default_group = snapshot.group_for_route(DefaultRoute())
    if default_group is None or not default_group.endpoint_ids:
        raise ValueError("Runtime composition requires a canonical default route")
    endpoint = snapshot.endpoint(default_group.endpoint_ids[0])
    if endpoint is None:
        raise ValueError("Runtime composition default route has no endpoint")
    bound_gateway = GenerationBoundRuntimeModelGateway(executor, generation, runtime_generation_id.value)
    gateway = gateway_decorator(bound_gateway) if gateway_decorator is not None else bound_gateway
    composition = RuntimeCompositionGeneration(
        runtime_generation_id=runtime_generation_id,
        topology_revision=generation.revision,
        gateway=gateway,
        route_policy=ModelRoutePolicy(generation),
        default_model=DefaultModelMetadata(
            model=endpoint.model,
            provider=endpoint.provider,
            transport=endpoint.transport,
            context_tokens=endpoint.capabilities.context_tokens,
        ),
        command_runtime=generation.command_runtime,
        session_runtime=generation.session_runtime,
        transfer_runtime=generation.transfer_runtime,
        permit_issuer=generation.permit_issuer,
        permit_audience=generation.permit_audience,
        generation_id=generation.generation_id,
        generation_artifact_digest=generation.generation_artifact_digest,
        artifact_store=artifact_store,
        artifact_reader=artifact_reader,
        _runtime_generation=generation,
    )
    return SharedRuntimeCompositionHandle(composition, reuse_key=reuse_key)


__all__ = [
    "DefaultModelMetadata",
    "LeaseReleasedError",
    "ModelRoutePolicy",
    "RuntimeCompositionGeneration",
    "RuntimeCompositionLease",
    "SharedRuntimeCompositionHandle",
    "build_runtime_composition",
]
