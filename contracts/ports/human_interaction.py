"""Port used by HandoffCoordinator to open a host-native Runtime modal."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.handoff import DriverHandoffHandle, HandoffRequest, HumanHandoffOutcome
from mote.contracts.surfaces import LiveSurfaceSession


@runtime_checkable
class HumanInteractionPort(Protocol):
    async def open_handoff(
        self,
        request: HandoffRequest,
        handle: DriverHandoffHandle,
        surface: LiveSurfaceSession | None = None,
    ) -> HumanHandoffOutcome:
        ...


__all__ = ["HumanInteractionPort"]
