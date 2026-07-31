"""Reusable presentation lifecycle for standalone live-surface windows."""
from __future__ import annotations

import asyncio
import contextlib

from mote.contracts.ports.surface.window import LiveWindowBackend, LiveWindowBackendSession
from mote.contracts.surface import LiveSurfaceSession, SurfacePresentationMode


class LiveWindowPresentationSession:
    """Retain one viewer while Handoff input authority comes and goes."""

    def __init__(
        self,
        backend: LiveWindowBackendSession,
        *,
        surface_kind: str,
        media_type: str,
    ) -> None:
        self._backend = backend
        self._surface_kind = surface_kind
        self._media_type = media_type
        self._surface: LiveSurfaceSession | None = None
        self._sequence = -1
        self._mirror_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self._backend.closed

    async def attach(self, surface: LiveSurfaceSession) -> None:
        if self.closed:
            raise RuntimeError(f"{self._surface_kind} window presentation is closed")
        self._validate_surface(surface)
        await self._detach_surface()
        frame = await surface.snapshot()
        self._validate_frame(frame.media_type)
        await self._backend.replace_frame(frame)
        self._surface = surface
        self._sequence = frame.sequence
        await self._backend.set_input_handler(surface.send)
        self._mirror_task = asyncio.create_task(self._mirror(surface))

    async def focus(self) -> None:
        if self.closed:
            raise RuntimeError(f"{self._surface_kind} window presentation is closed")
        await self._backend.focus()

    async def synchronize(self) -> None:
        if self.closed or self._surface is None:
            raise RuntimeError(f"{self._surface_kind} window presentation is closed")

    async def release(self) -> None:
        if self.closed:
            return
        await self._backend.set_input_handler(None)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._backend.set_input_handler(None)
        await self._detach_surface()
        await self._backend.aclose()

    async def _detach_surface(self) -> None:
        await self._backend.set_input_handler(None)
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
                self._validate_frame(frame.media_type)
                await self._backend.replace_frame(frame)
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

    def _validate_surface(self, surface: LiveSurfaceSession) -> None:
        if surface.descriptor.kind != self._surface_kind:
            raise ValueError(f"expected {self._surface_kind!r} surface, got {surface.descriptor.kind!r}")

    def _validate_frame(self, media_type: str) -> None:
        if media_type != self._media_type:
            raise ValueError(f"unsupported {self._surface_kind} surface media type: {media_type}")


class LiveWindowPresenter:
    """Bind one Runtime Surface kind to a standalone viewer backend."""

    presentation = SurfacePresentationMode.WINDOW

    def __init__(
        self,
        backend: LiveWindowBackend,
        *,
        surface_kind: str,
        media_type: str,
    ) -> None:
        if not surface_kind or not media_type:
            raise ValueError("surface_kind and media_type must be non-empty")
        self._backend = backend
        self._surface_kind = surface_kind
        self._media_type = media_type
        self.surface_kinds = frozenset({surface_kind})

    async def present(self, surface: LiveSurfaceSession) -> LiveWindowPresentationSession:
        if surface.descriptor.kind != self._surface_kind:
            raise ValueError(f"expected {self._surface_kind!r} surface, got {surface.descriptor.kind!r}")
        frame = await surface.snapshot()
        if frame.media_type != self._media_type:
            raise ValueError(f"unsupported {self._surface_kind} surface media type: {frame.media_type}")
        backend = await self._backend.open(surface.descriptor, frame)
        presentation = LiveWindowPresentationSession(
            backend,
            surface_kind=self._surface_kind,
            media_type=self._media_type,
        )
        try:
            await presentation.attach(surface)
        except BaseException:
            await presentation.aclose()
            raise
        return presentation


__all__ = ["LiveWindowPresentationSession", "LiveWindowPresenter"]
