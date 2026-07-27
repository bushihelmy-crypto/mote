"""Runtime-side adapter exposing a driver's live surface to host ports."""
from __future__ import annotations

from mote.contracts.handoff import DriverHandoffHandle
from mote.contracts.ports.runtime_driver import LiveSurfaceRuntimeDriver, ObservableSurfaceRuntimeDriver
from mote.contracts.surfaces import SurfaceFrame, SurfaceInput


class RuntimeLiveSurfaceSession:
    """Bind one opaque driver handle to the host-neutral surface protocol."""

    def __init__(self, driver: LiveSurfaceRuntimeDriver, handle: DriverHandoffHandle) -> None:
        self._driver = driver
        self._handle = handle
        self.descriptor = handle.surface

    async def snapshot(self) -> SurfaceFrame:
        return await self._driver.snapshot_surface(self._handle)

    async def send(self, event: SurfaceInput) -> None:
        await self._driver.send_surface_input(self._handle, event)

    async def next_frame(self, after_sequence: int) -> SurfaceFrame | None:
        if isinstance(self._driver, ObservableSurfaceRuntimeDriver):
            return await self._driver.next_surface_frame(self._handle, after_sequence)
        frame = await self.snapshot()
        return frame if frame.sequence > after_sequence else None

    async def aclose(self) -> None:
        if isinstance(self._driver, ObservableSurfaceRuntimeDriver):
            await self._driver.detach_surface(self._handle)


__all__ = ["RuntimeLiveSurfaceSession"]
