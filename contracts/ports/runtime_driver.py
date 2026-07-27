"""Narrow lifecycle face implemented by every managed runtime driver."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.handoff import DriverHandoffHandle, DriverHandoffResult, HandoffRequest, HumanHandoffOutcome
from mote.contracts.runtimes import (
    DriverCheckpoint,
    DriverStartResult,
    RuntimeCapabilities,
    RuntimeCheckpoint,
    RuntimeHealth,
    RuntimeOperationIntent,
)
from mote.contracts.surfaces import SurfaceFrame, SurfaceInput


@runtime_checkable
class ManagedRuntimeDriver(Protocol):
    kind: str
    capabilities: RuntimeCapabilities

    async def start(self, checkpoint: RuntimeCheckpoint | None = None) -> DriverStartResult:
        ...

    async def health(self) -> RuntimeHealth:
        ...

    async def checkpoint(self, reason: str) -> DriverCheckpoint:
        ...

    async def aclose(self) -> None:
        ...


@runtime_checkable
class HandoffRuntimeDriver(ManagedRuntimeDriver, Protocol):
    async def prepare_handoff(self, request: HandoffRequest) -> DriverHandoffHandle:
        ...

    async def finish_handoff(
        self,
        handle: DriverHandoffHandle,
        outcome: HumanHandoffOutcome,
    ) -> DriverHandoffResult:
        ...


@runtime_checkable
class LiveSurfaceRuntimeDriver(Protocol):
    async def snapshot_surface(self, handle: DriverHandoffHandle) -> SurfaceFrame:
        ...

    async def send_surface_input(self, handle: DriverHandoffHandle, event: SurfaceInput) -> None:
        ...


@runtime_checkable
class ObservableSurfaceRuntimeDriver(Protocol):
    """Optional long-lived observation face for a live Runtime surface."""

    async def next_surface_frame(
        self,
        handle: DriverHandoffHandle,
        after_sequence: int,
    ) -> SurfaceFrame | None:
        ...

    async def detach_surface(self, handle: DriverHandoffHandle) -> None:
        ...


@runtime_checkable
class JournaledRuntimeDriver(ManagedRuntimeDriver, Protocol):
    """Opt-in deterministic replay face; external side-effect drivers omit it."""

    async def replay_operation(self, intent: RuntimeOperationIntent) -> None:
        ...


__all__ = [
    "HandoffRuntimeDriver",
    "LiveSurfaceRuntimeDriver",
    "JournaledRuntimeDriver",
    "ManagedRuntimeDriver",
    "ObservableSurfaceRuntimeDriver",
]
