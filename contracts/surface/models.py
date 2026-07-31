"""Host-neutral contracts for live interactive Runtime surfaces."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class SurfacePresentationMode(StrEnum):
    """Host placement requested by a live surface."""

    EMBEDDED = "embedded"
    WINDOW = "window"


@dataclass(frozen=True, slots=True)
class SurfaceDescriptor:
    """Stable identity and presentation requirements for one live surface."""

    kind: str
    ref: str
    presentation: SurfacePresentationMode = SurfacePresentationMode.EMBEDDED
    title: str = ""


@dataclass(frozen=True, slots=True)
class SurfaceFrame:
    """One complete renderable snapshot of a live Runtime surface."""

    sequence: int
    media_type: str
    content: str
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SurfaceInput:
    """One host-originated interaction delivered to a live Runtime surface."""

    kind: str
    data: str = ""
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@runtime_checkable
class LiveSurfaceSession(Protocol):
    """One presentation attachment to a Runtime surface.

    Input authority may expire when handoff ends, while observation can remain
    attached until the presentation or Runtime closes.
    """

    descriptor: SurfaceDescriptor

    async def snapshot(self) -> SurfaceFrame:
        ...

    async def send(self, event: SurfaceInput) -> None:
        ...

    async def next_frame(self, after_sequence: int) -> SurfaceFrame | None:
        ...

    async def aclose(self) -> None:
        ...


__all__ = [
    "LiveSurfaceSession",
    "SurfaceDescriptor",
    "SurfaceFrame",
    "SurfaceInput",
    "SurfacePresentationMode",
]
