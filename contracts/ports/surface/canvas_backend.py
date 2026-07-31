"""Backend SPI for high-fidelity Canvas editors and renderers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from mote.contracts.surface import CanvasDocument, CanvasExportRepresentation, CanvasOperation


@dataclass(frozen=True, slots=True)
class CanvasBackendCapabilities:
    """Declarative backend features used for selection and negotiation."""

    native_window: bool
    human_editing: bool
    screenshots: bool
    import_formats: frozenset[str] = field(default_factory=frozenset)
    export_formats: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class CanvasBackendRender:
    mime_type: str
    content: bytes


@runtime_checkable
class CanvasBackendSession(Protocol):
    """One visible editor instance owned by a Canvas backend."""

    @property
    def closed(self) -> bool:
        ...

    async def focus(self) -> None:
        ...

    async def set_human_editable(self, editable: bool) -> None:
        ...

    async def apply_delta(self, operations: tuple[CanvasOperation, ...]) -> None:
        ...

    async def replace_scene(self, scene: CanvasDocument) -> None:
        ...

    async def snapshot_scene(self) -> CanvasDocument:
        ...

    async def wait_closed(self) -> None:
        ...

    async def render(self) -> CanvasBackendRender:
        ...

    async def export(self, format: str) -> CanvasExportRepresentation:
        ...

    async def aclose(self) -> None:
        ...


@runtime_checkable
class CanvasBackend(Protocol):
    """Export canonical scenes or open them in a concrete editor."""

    name: str
    capabilities: CanvasBackendCapabilities

    async def open(self, scene: CanvasDocument) -> CanvasBackendSession:
        ...

    async def export(
        self,
        scene: CanvasDocument,
        format: str,
    ) -> CanvasExportRepresentation:
        ...


@runtime_checkable
class CanvasExportPort(Protocol):
    """Batch export of one immutable canonical scene without presentation."""

    async def export(
        self,
        document: CanvasDocument,
        formats: tuple[str, ...],
    ) -> tuple[CanvasExportRepresentation, ...]:
        ...


__all__ = [
    "CanvasBackend",
    "CanvasBackendCapabilities",
    "CanvasBackendRender",
    "CanvasBackendSession",
    "CanvasExportPort",
]
