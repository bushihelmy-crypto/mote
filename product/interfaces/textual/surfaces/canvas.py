"""Independent-window presenter for canonical Canvas surfaces."""
from __future__ import annotations

import asyncio
import contextlib

from mote.contracts.ports.surface.canvas_backend import CanvasBackend, CanvasBackendSession
from mote.contracts.surface import CanvasDocument, LiveSurfaceSession, SurfaceInput, SurfacePresentationMode

_CANVAS_MEDIA_TYPE = "application/vnd.mote.canvas+json"


class CanvasWindowPresentationSession:
    """Keep one draw.io window attached across multiple handoff windows."""

    def __init__(self, backend: CanvasBackendSession) -> None:
        self._backend = backend
        self._surface: LiveSurfaceSession | None = None
        self._sequence = -1
        self._mirror_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self._backend.closed

    async def attach(self, surface: LiveSurfaceSession) -> None:
        if self.closed:
            raise RuntimeError("canvas window presentation is closed")
        await self._detach_surface()
        frame = await surface.snapshot()
        document = self._document_from_frame(frame.media_type, frame.content)
        await self._backend.replace_scene(document)
        self._surface = surface
        self._sequence = frame.sequence
        await self._backend.set_human_editable(True)
        self._mirror_task = asyncio.create_task(self._mirror(surface))

    async def focus(self) -> None:
        if self.closed:
            raise RuntimeError("canvas window presentation is closed")
        await self._backend.focus()

    async def synchronize(self) -> None:
        if self.closed or self._surface is None:
            raise RuntimeError("canvas window presentation is closed")
        document = await self._backend.snapshot_scene()
        await self._surface.send(SurfaceInput(kind="canvas.replace", data=document.model_dump_json()))

    async def release(self) -> None:
        if self.closed:
            return
        await self._backend.set_human_editable(False)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._detach_surface()
        await self._backend.aclose()

    async def _detach_surface(self) -> None:
        task, self._mirror_task = self._mirror_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        surface, self._surface = self._surface, None
        if surface is not None:
            await surface.aclose()

    async def _mirror(self, surface: LiveSurfaceSession) -> None:
        try:
            while surface is self._surface:
                if self.closed:
                    await self._close_from_monitor(surface)
                    return
                frame_wait = asyncio.create_task(surface.next_frame(self._sequence))
                window_wait = asyncio.create_task(self._backend.wait_closed())
                waits = (frame_wait, window_wait)
                try:
                    done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in waits:
                        if not task.done():
                            task.cancel()
                    for task in waits:
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                if window_wait in done:
                    await self._close_from_monitor(surface)
                    return
                frame = frame_wait.result()
                if frame is None:
                    await self._close_from_monitor(surface)
                    return
                document = self._document_from_frame(frame.media_type, frame.content)
                await self._backend.replace_scene(document)
                self._sequence = frame.sequence
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._close_from_monitor(surface)

    async def _close_from_monitor(self, surface: LiveSurfaceSession) -> None:
        self._closed = True
        if self._surface is surface:
            self._surface = None
            await surface.aclose()
        await self._backend.aclose()

    @staticmethod
    def _document_from_frame(media_type: str, content: str) -> CanvasDocument:
        if media_type != _CANVAS_MEDIA_TYPE:
            raise ValueError(f"unsupported Canvas surface media type: {media_type}")
        return CanvasDocument.model_validate_json(content)


class CanvasWindowPresenter:
    """Present Canvas only in a full native editor window."""

    presentation = SurfacePresentationMode.WINDOW
    surface_kinds = frozenset({"canvas"})

    def __init__(self, backend: CanvasBackend) -> None:
        if not backend.capabilities.native_window or not backend.capabilities.human_editing:
            raise ValueError("Canvas window presenter requires an editable native-window backend")
        self._backend = backend

    async def present(self, surface: LiveSurfaceSession) -> CanvasWindowPresentationSession:
        frame = await surface.snapshot()
        if frame.media_type != _CANVAS_MEDIA_TYPE:
            raise ValueError(f"unsupported Canvas surface media type: {frame.media_type}")
        document = CanvasDocument.model_validate_json(frame.content)
        backend = await self._backend.open(document)
        presentation = CanvasWindowPresentationSession(backend)
        try:
            await presentation.attach(surface)
        except BaseException:
            await presentation.aclose()
            raise
        return presentation


__all__ = ["CanvasWindowPresentationSession", "CanvasWindowPresenter"]
