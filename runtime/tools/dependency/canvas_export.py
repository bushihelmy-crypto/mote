"""Headless Canvas export routing independent of live presenter sessions."""
from __future__ import annotations

import asyncio

from mote.contracts.canvas import CanvasDocument, CanvasExportRepresentation
from mote.contracts.ports.canvas_backend import CanvasBackend
from mote.runtime.tools.dependency.canvas_backends import DrawioCanvasBackend, NativeCanvasBackend


class CanvasExportService:
    """Select exporters by format and execute one immutable snapshot batch."""

    def __init__(self, backends: tuple[CanvasBackend, ...] | None = None) -> None:
        selected = backends or (NativeCanvasBackend(), DrawioCanvasBackend())
        exporters: dict[str, CanvasBackend] = {}
        for backend in selected:
            for format in backend.capabilities.export_formats:
                normalized = format.lower().lstrip(".")
                if normalized in exporters:
                    raise ValueError(f"duplicate Canvas exporter for {normalized!r}")
                exporters[normalized] = backend
        self._exporters = exporters

    async def export(
        self,
        document: CanvasDocument,
        formats: tuple[str, ...],
    ) -> tuple[CanvasExportRepresentation, ...]:
        normalized = tuple(format.lower().lstrip(".") for format in formats)
        if not normalized or any(not format for format in normalized):
            raise ValueError("Canvas export formats must be non-empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Canvas export formats must be unique")
        try:
            backends = tuple(self._exporters[format] for format in normalized)
        except KeyError as exc:
            raise ValueError(f"unsupported Canvas export format: {exc.args[0]}") from exc
        exports = await asyncio.gather(
            *(backend.export(document.model_copy(deep=True), format) for backend, format in zip(backends, normalized))
        )
        return tuple(exports)


__all__ = ["CanvasExportService"]
