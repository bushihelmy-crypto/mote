"""Standalone Chromium window shell for pluggable live-surface frontends."""
from __future__ import annotations

import asyncio
from typing import Any

try:
    from playwright.async_api import async_playwright
except ImportError as _playwright_import_error:
    async_playwright = None
else:
    _playwright_import_error = None

from mote.contracts.ports.surface.window import SurfaceInputHandler
from mote.contracts.surface import SurfaceDescriptor, SurfaceFrame, SurfaceInput
from mote.runtime.interactive.cdp import focus_chromium_page
from mote.runtime.interactive.chromium_frontends import DEFAULT_CHROMIUM_FRONTENDS, ChromiumSurfaceFrontend


class LiveWindowBackendUnavailableError(RuntimeError):
    """The standalone Chromium viewer cannot be opened."""


class ChromiumLiveWindowBackend:
    """Open a dedicated Chromium application window for a live Surface."""

    def __init__(
        self,
        frontends: tuple[ChromiumSurfaceFrontend, ...] = DEFAULT_CHROMIUM_FRONTENDS,
    ) -> None:
        self._frontends: dict[str, ChromiumSurfaceFrontend] = {}
        for frontend in frontends:
            for media_type in frontend.media_types:
                if media_type in self._frontends:
                    raise ValueError(f"duplicate Chromium frontend media type: {media_type}")
                self._frontends[media_type] = frontend

    async def open(
        self,
        descriptor: SurfaceDescriptor,
        frame: SurfaceFrame,
    ) -> "ChromiumLiveWindowSession":
        session = ChromiumLiveWindowSession(descriptor, self._frontends)
        await session.start(frame)
        return session


class ChromiumLiveWindowSession:
    """One Chromium viewer whose input callback can be fenced independently."""

    def __init__(
        self,
        descriptor: SurfaceDescriptor,
        frontends: dict[str, ChromiumSurfaceFrontend],
    ) -> None:
        self._descriptor = descriptor
        self._frontends = dict(frontends)
        self._frontend: ChromiumSurfaceFrontend | None = None
        self._manager: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._input_handler: SurfaceInputHandler | None = None
        self._input_lock = asyncio.Lock()
        self._window_closed = asyncio.Event()

    @property
    def closed(self) -> bool:
        browser = self._browser
        page = self._page
        return browser is None or not browser.is_connected() or page is None or page.is_closed()

    async def start(self, frame: SurfaceFrame) -> None:
        if async_playwright is None:
            raise LiveWindowBackendUnavailableError(
                "Playwright is required for standalone live-surface windows"
            ) from _playwright_import_error
        frontend = self._frontends.get(frame.media_type)
        if frontend is None:
            raise ValueError(f"unsupported live-window media type: {frame.media_type}")
        self._frontend = frontend
        self._manager = async_playwright()
        try:
            playwright = await self._manager.start()
            self._browser = await playwright.chromium.launch(
                headless=False,
                args=["--start-maximized"],
            )
            self._browser.on("disconnected", lambda *_: self._window_closed.set())
            self._context = await self._browser.new_context(no_viewport=True)
            self._page = await self._context.new_page()
            self._page.once("close", lambda *_: self._window_closed.set())
            await self._page.expose_binding("__moteInput", self._receive_input)
            await self._page.set_content(frontend.document())
            for script in frontend.scripts():
                await self._page.add_script_tag(content=script)
            await self.replace_frame(frame)
            await self.set_input_handler(None)
            await self.focus()
        except Exception as exc:
            await self.aclose()
            if isinstance(exc, (LiveWindowBackendUnavailableError, ValueError)):
                raise
            raise LiveWindowBackendUnavailableError(f"live-surface window failed to start: {exc}") from exc

    async def focus(self) -> None:
        if self.closed:
            raise RuntimeError("live-surface window is closed")
        await focus_chromium_page(self._page)

    async def replace_frame(self, frame: SurfaceFrame) -> None:
        if self.closed:
            raise RuntimeError("live-surface window is closed")
        if self._frontend is None or frame.media_type not in self._frontend.media_types:
            raise ValueError(f"unsupported live-window media type: {frame.media_type}")
        await self._page.evaluate(
            "frame => window.__moteRender(frame)",
            {
                "kind": self._descriptor.kind,
                "title": self._descriptor.title,
                "mediaType": frame.media_type,
                "content": frame.content,
                "sequence": frame.sequence,
                "metadata": dict(frame.metadata),
            },
        )

    async def set_input_handler(self, handler: SurfaceInputHandler | None) -> None:
        async with self._input_lock:
            self._input_handler = handler
            if not self.closed:
                await self._page.evaluate(
                    "editable => window.__moteSetEditable(editable)",
                    handler is not None,
                )

    async def wait_closed(self) -> None:
        if self.closed:
            return
        await self._window_closed.wait()

    async def aclose(self) -> None:
        async with self._input_lock:
            self._input_handler = None
        manager, browser = self._manager, self._browser
        self._manager = None
        self._browser = None
        self._context = None
        self._page = None
        self._window_closed.set()
        if browser is not None and browser.is_connected():
            await browser.close()
        if manager is not None:
            await manager.__aexit__(None, None, None)

    async def _receive_input(self, _source: Any, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        async with self._input_lock:
            handler = self._input_handler
            if handler is None:
                return
            kind = str(payload.get("kind", ""))
            if not kind:
                return
        # A notebook execution may stay open while the kernel waits for stdin.
        # Do not hold the handler lock across that call: a second browser binding
        # invocation must be able to deliver the fenced input reply.
        await handler(SurfaceInput(kind=kind, data=str(payload.get("data", ""))))


__all__ = [
    "ChromiumLiveWindowBackend",
    "ChromiumLiveWindowSession",
    "LiveWindowBackendUnavailableError",
]
