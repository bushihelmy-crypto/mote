"""Deterministic in-process Canvas backend for headless rendering."""
from __future__ import annotations

import asyncio

from mote.contracts.ports.surface.canvas_backend import CanvasBackendCapabilities, CanvasBackendRender
from mote.contracts.surface import CanvasDocument, CanvasExportRepresentation, CanvasOperation
from mote.runtime.interactive.canvas.state import apply_canvas_operations
from mote.runtime.interactive.canvas.svg import render_canvas_svg


class NativeCanvasBackend:
    name = "native-svg"
    capabilities = CanvasBackendCapabilities(
        native_window=False,
        human_editing=False,
        screenshots=False,
        export_formats=frozenset({"svg"}),
    )

    async def open(self, scene: CanvasDocument) -> "NativeCanvasSession":
        return NativeCanvasSession(scene)

    async def export(
        self,
        scene: CanvasDocument,
        format: str,
    ) -> CanvasExportRepresentation:
        normalized = format.lower().lstrip(".")
        if normalized != "svg":
            raise ValueError(f"native Canvas backend cannot export {format!r}")
        return CanvasExportRepresentation(
            representation="svg",
            mime_type="image/svg+xml",
            content=render_canvas_svg(scene).encode("utf-8"),
            suggested_name="canvas.svg",
        )


class NativeCanvasSession:
    def __init__(self, scene: CanvasDocument) -> None:
        self._scene = scene.model_copy(deep=True)
        self._closed = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def focus(self) -> None:
        return None

    async def set_human_editable(self, editable: bool) -> None:
        if editable:
            raise RuntimeError("native Canvas backend has no human-editable window")

    async def apply_delta(self, operations: tuple[CanvasOperation, ...]) -> None:
        self._scene, _, _ = apply_canvas_operations(self._scene, operations)

    async def replace_scene(self, scene: CanvasDocument) -> None:
        self._scene = scene.model_copy(deep=True)

    async def snapshot_scene(self) -> CanvasDocument:
        return self._scene.model_copy(deep=True)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def render(self) -> CanvasBackendRender:
        return CanvasBackendRender(
            mime_type="image/svg+xml",
            content=render_canvas_svg(self._scene).encode("utf-8"),
        )

    async def export(self, format: str) -> CanvasExportRepresentation:
        return await NativeCanvasBackend().export(self._scene, format)

    async def aclose(self) -> None:
        self._closed.set()


__all__ = ["NativeCanvasBackend", "NativeCanvasSession"]
