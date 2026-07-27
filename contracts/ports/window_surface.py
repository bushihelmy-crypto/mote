"""Backend SPI for standalone live-surface viewer windows."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from mote.contracts.surfaces import SurfaceDescriptor, SurfaceFrame, SurfaceInput

SurfaceInputHandler = Callable[[SurfaceInput], Awaitable[None]]


@runtime_checkable
class LiveWindowBackendSession(Protocol):
    """One independently closable viewer window for a live Surface."""

    @property
    def closed(self) -> bool:
        ...

    async def focus(self) -> None:
        ...

    async def replace_frame(self, frame: SurfaceFrame) -> None:
        ...

    async def set_input_handler(self, handler: SurfaceInputHandler | None) -> None:
        ...

    async def wait_closed(self) -> None:
        ...

    async def aclose(self) -> None:
        ...


@runtime_checkable
class LiveWindowBackend(Protocol):
    """Open a host window that renders SurfaceFrames and emits SurfaceInputs."""

    async def open(
        self,
        descriptor: SurfaceDescriptor,
        frame: SurfaceFrame,
    ) -> LiveWindowBackendSession:
        ...


__all__ = ["LiveWindowBackend", "LiveWindowBackendSession", "SurfaceInputHandler"]
