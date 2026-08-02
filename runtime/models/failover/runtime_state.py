"""Immutable model Runtime generation value."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mote.contracts.inference.epochs import ExecutionEpochSource
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.ports.artifact.provider_transfer import ProviderArtifactTransferRuntime
from mote.contracts.ports.inference.session_runtime import SessionRuntime
from mote.contracts.ports.inference.wire_permit import WirePermitIssuer
from mote.contracts.ports.model.endpoint_binding import ModelEndpointBindingResolver
from mote.contracts.ports.service.command_runtime import ServiceCommandRuntime
from mote.runtime.models.failover.planner import FailoverPlanner
from mote.runtime.models.inference_attempt_executor import InferenceAttemptExecutor


class AsyncGenerationResource(Protocol):
    """One explicitly owned asynchronous resource in a model generation."""

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class ModelRuntimeGeneration:
    planner: FailoverPlanner
    binding_resolver: ModelEndpointBindingResolver | None = None
    attempt_executor: InferenceAttemptExecutor | None = None
    command_runtime: ServiceCommandRuntime | None = None
    session_runtime: SessionRuntime | None = None
    transfer_runtime: ProviderArtifactTransferRuntime | None = None
    permit_issuer: WirePermitIssuer | None = None
    epoch_source: ExecutionEpochSource | None = None
    permit_audience: str = ""
    generation_id: str = ""
    generation_artifact_digest: str = ""
    principal: InferencePrincipal | None = None
    scheduling: TrustedSchedulingClass | None = None
    closeables: tuple[AsyncGenerationResource, ...] = ()

    @property
    def revision(self) -> str:
        return self.planner.snapshot.revision


__all__ = ["AsyncGenerationResource", "ModelRuntimeGeneration"]
