"""Cross-layer contracts for atomic application composition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, Union

from mote.contracts.ports.model.gateway import ModelGateway


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
    def application_generation_id(self) -> ApplicationGenerationId:
        ...

    async def acquire_runtime(self) -> "RuntimeCompositionLeasePort":
        ...

    async def aclose(self) -> None:
        ...


class ApplicationCompositionPort(Protocol):
    async def acquire(self) -> ApplicationLeasePort:
        ...


class ApplicationReloadPort(Protocol):
    async def reload(self) -> ActivationReceipt:
        ...


class RuntimeCompositionLeasePort(Protocol):
    @property
    def runtime_generation_id(self) -> RuntimeGenerationId:
        ...

    @property
    def topology_revision(self) -> str:
        ...

    @property
    def gateway(self) -> ModelGateway:
        ...

    @property
    def route_policy(self) -> Any:
        ...

    @property
    def default_model(self) -> Any:
        ...

    @property
    def command_runtime(self) -> Any:
        ...

    @property
    def session_runtime(self) -> Any:
        ...

    @property
    def transfer_runtime(self) -> Any:
        ...

    @property
    def permit_issuer(self) -> Any:
        ...

    @property
    def permit_audience(self) -> str:
        ...

    @property
    def generation_id(self) -> str:
        ...

    @property
    def generation_artifact_digest(self) -> str:
        ...

    @property
    def artifact_store(self) -> Any:
        ...

    @property
    def artifact_reader(self) -> Any:
        ...

    async def aclose(self) -> None:
        ...


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
    "ExpectedActive",
    "ExpectedApplicationState",
    "ExpectedEmpty",
    "ExpectedStateMismatchError",
    "ReloadSequence",
    "RetiredGenerationCapacityError",
    "RuntimeGenerationId",
    "RuntimeCompositionLeasePort",
    "RuntimeRoleConfigView",
    "SourceRevision",
    "StaleReloadError",
]
