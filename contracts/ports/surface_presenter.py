"""Host-neutral presentation port for live Runtime surfaces."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.surfaces import LiveSurfaceSession, SurfacePresentationMode


@runtime_checkable
class SurfacePresentationSession(Protocol):
    """One reusable host presentation attached to a live Runtime surface."""

    @property
    def closed(self) -> bool:
        ...

    async def attach(self, surface: LiveSurfaceSession) -> None:
        ...

    async def focus(self) -> None:
        ...

    async def synchronize(self) -> None:
        ...

    async def release(self) -> None:
        ...

    async def aclose(self) -> None:
        ...


@runtime_checkable
class LiveSurfacePresenter(Protocol):
    """Open one or more surface kinds in one host placement."""

    presentation: SurfacePresentationMode
    surface_kinds: frozenset[str]

    async def present(self, surface: LiveSurfaceSession) -> SurfacePresentationSession:
        ...


__all__ = ["LiveSurfacePresenter", "SurfacePresentationSession"]
