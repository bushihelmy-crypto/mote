"""Cross-layer contracts for atomic application composition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Union

from mote.contracts.inference.epochs import ExecutionEpochSource
from mote.contracts.model.failover import EndpointDescriptor
from mote.contracts.model.topology import RouteId
from mote.contracts.ports.artifact.provider_transfer import ProviderArtifactTransferRuntime
from mote.contracts.ports.artifact.store import ArtifactLookupIndex, GenerationArtifactReader
from mote.contracts.ports.inference.session_runtime import SessionRuntime
from mote.contracts.ports.inference.wire_permit import WirePermitIssuer
from mote.contracts.ports.model.gateway import ModelGateway
from mote.contracts.ports.service.command_runtime import ServiceCommandRuntime


class ApplicationState(str, Enum):
    EMPTY = "empty"
    ACTIVE = "active"
    SHUTTING_DOWN = "shutting_down"
    CLOSED = "closed"


class ApplicationHealth(str, Enum):
    NOT_READY = "not_ready"
    READY = "ready"
    DEGRADED = "degraded"
    SHUTTING_DOWN = "shutting_down"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ApplicationGenerationId:
    value: str


@dataclass(frozen=True, slots=True)
class RuntimeGenerationId:
    value: str


@dataclass(frozen=True, slots=True)
class RuntimeLeaseHolderId:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("Runtime lease holder identity cannot be empty")


class RuntimeLeaseReleaseDisposition(str, Enum):
    RELEASED = "released"
    ALREADY_RELEASED = "already_released"
    TRANSFERRED = "transferred"


@dataclass(frozen=True, slots=True)
class RuntimeLeaseReleaseReceipt:
    runtime_generation_id: RuntimeGenerationId
    holder_id: RuntimeLeaseHolderId
    disposition: RuntimeLeaseReleaseDisposition


@dataclass(frozen=True, slots=True)
class RuntimeLeaseTransferReceipt:
    runtime_generation_id: RuntimeGenerationId
    previous_holder_id: RuntimeLeaseHolderId
    holder_id: RuntimeLeaseHolderId


@dataclass(frozen=True, slots=True)
class SourceRevision:
    value: str


@dataclass(frozen=True, slots=True)
class ReloadSequence:
    value: int

    def __post_init__(self) -> None:
        if self.value < 1:
            raise ValueError("reload sequence must be positive")


@dataclass(frozen=True, slots=True)
class RuntimeRoleConfigView:
    """Canonical Runtime-visible subset of one Product role configuration."""

    response_language: str


@dataclass(frozen=True, slots=True)
class DefaultModelView:
    model: str
    provider: str
    transport: str
    context_tokens: int


class ModelRoutePolicyPort(Protocol):
    def supports(self, route_id: RouteId) -> bool: ...

    def profile(self, route_id: RouteId) -> EndpointDescriptor | None: ...


@dataclass(frozen=True, slots=True)
class ExpectedEmpty:
    pass


@dataclass(frozen=True, slots=True)
class ExpectedActive:
    generation_id: ApplicationGenerationId


ExpectedApplicationState = Union[ExpectedEmpty, ExpectedActive]


@dataclass(frozen=True, slots=True)
class ActivationToken:
    value: str


@dataclass(frozen=True, slots=True)
class ActivationReceipt:
    application_generation_id: ApplicationGenerationId
    runtime_generation_id: RuntimeGenerationId
    source_revision: SourceRevision
    reload_sequence: ReloadSequence


class ApplicationNotReadyError(RuntimeError):
    pass


class ApplicationShuttingDownError(RuntimeError):
    pass


class ApplicationClosedError(RuntimeError):
    pass


class ExpectedStateMismatchError(RuntimeError):
    pass


class StaleReloadError(RuntimeError):
    pass


class RetiredGenerationCapacityError(RuntimeError):
    pass


class ApplicationLeasePort(Protocol):
    @property
    def runtime_role_config(self) -> RuntimeRoleConfigView: ...

    @property
    def application_generation_id(self) -> ApplicationGenerationId: ...

    async def acquire_runtime(self) -> "RuntimeCompositionLeasePort": ...

    async def aclose(self) -> None: ...


class ApplicationCompositionPort(Protocol):
    async def acquire(self) -> ApplicationLeasePort: ...


class ApplicationReloadPort(Protocol):
    async def reload(self) -> ActivationReceipt: ...


class RuntimeCompositionLeasePort(Protocol):
    @property
    def runtime_generation_id(self) -> RuntimeGenerationId: ...

    @property
    def holder_id(self) -> RuntimeLeaseHolderId: ...

    @property
    def topology_revision(self) -> str: ...

    @property
    def gateway(self) -> ModelGateway: ...

    @property
    def route_policy(self) -> ModelRoutePolicyPort: ...

    @property
    def default_model(self) -> DefaultModelView: ...

    @property
    def command_runtime(self) -> ServiceCommandRuntime | None: ...

    @property
    def session_runtime(self) -> SessionRuntime | None: ...

    @property
    def transfer_runtime(self) -> ProviderArtifactTransferRuntime | None: ...

    @property
    def permit_issuer(self) -> WirePermitIssuer | None: ...

    @property
    def epoch_source(self) -> ExecutionEpochSource | None: ...

    @property
    def permit_audience(self) -> str: ...

    @property
    def generation_id(self) -> str: ...

    @property
    def generation_artifact_digest(self) -> str: ...

    @property
    def artifact_store(self) -> ArtifactLookupIndex | None: ...

    @property
    def artifact_reader(self) -> GenerationArtifactReader | None: ...

    async def transfer(
        self, holder_id: RuntimeLeaseHolderId
    ) -> tuple["RuntimeCompositionLeasePort", RuntimeLeaseTransferReceipt]: ...

    async def aclose(self) -> RuntimeLeaseReleaseReceipt: ...


__all__ = [
    "ActivationReceipt",
    "ActivationToken",
    "ApplicationClosedError",
    "ApplicationCompositionPort",
    "ApplicationGenerationId",
    "ApplicationHealth",
    "ApplicationLeasePort",
    "ApplicationNotReadyError",
    "ApplicationReloadPort",
    "ApplicationShuttingDownError",
    "ApplicationState",
    "DefaultModelView",
    "ExpectedActive",
    "ExpectedApplicationState",
    "ExpectedEmpty",
    "ExpectedStateMismatchError",
    "ReloadSequence",
    "RetiredGenerationCapacityError",
    "RuntimeGenerationId",
    "RuntimeLeaseHolderId",
    "RuntimeLeaseReleaseDisposition",
    "RuntimeLeaseReleaseReceipt",
    "RuntimeLeaseTransferReceipt",
    "ModelRoutePolicyPort",
    "RuntimeCompositionLeasePort",
    "RuntimeRoleConfigView",
    "SourceRevision",
    "StaleReloadError",
]
