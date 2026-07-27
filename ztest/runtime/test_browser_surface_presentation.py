from __future__ import annotations

import asyncio
import json

import pytest

from mote.contracts.notebook import NOTEBOOK_MEDIA_TYPE
from mote.contracts.surfaces import SurfaceDescriptor, SurfaceFrame, SurfaceInput, SurfacePresentationMode
from mote.product.cli.surfaces.browser import BrowserWindowPresenter
from mote.product.cli.surfaces.device import DeviceWindowPresenter
from mote.product.cli.surfaces.jupyter import JupyterWindowPresenter
from mote.product.cli.surfaces.live_window import LiveWindowPresentationSession
from mote.product.cli.surfaces.terminal import TerminalWindowPresenter
from mote.runtime.interactive.presentation import SurfacePresenterRegistry


class _Surface:
    def __init__(
        self,
        label: str = "first",
        *,
        kind: str = "browser",
        media_type: str = "application/vnd.mote.browser+json",
    ) -> None:
        self.descriptor = SurfaceDescriptor(
            kind=kind,
            ref=f"{kind}:test",
            presentation=SurfacePresentationMode.WINDOW,
            title=kind.title(),
        )
        self.media_type = media_type
        self.label = label
        self.sequence = 1
        self.sent = []
        self.detached = False
        self._changed = asyncio.Event()
        self._detached = asyncio.Event()

    async def snapshot(self):
        return SurfaceFrame(
            sequence=self.sequence,
            media_type=self.media_type,
            content=json.dumps({"tabs": self.label, "screenshot_b64": "ZmFrZQ=="}),
        )

    async def send(self, event):
        self.sent.append(event)

    async def next_frame(self, after_sequence):
        while not self.detached and self.sequence <= after_sequence:
            self._changed.clear()
            await self._changed.wait()
        return None if self.detached else await self.snapshot()

    async def aclose(self):
        self.detached = True
        self._changed.set()
        self._detached.set()

    async def wait_detached(self):
        await self._detached.wait()

    def publish(self, label: str):
        self.label = label
        self.sequence += 1
        self._changed.set()


class _BackendSession:
    def __init__(self, frame) -> None:
        self.frame = frame
        self.focused = False
        self.closed = False
        self.input_handler = None
        self._closed = asyncio.Event()
        self.updated = asyncio.Event()

    async def focus(self):
        self.focused = True

    async def replace_frame(self, frame):
        self.frame = frame
        self.updated.set()

    async def set_input_handler(self, handler):
        self.input_handler = handler

    async def wait_closed(self):
        await self._closed.wait()

    async def aclose(self):
        self.input_handler = None
        self.closed = True
        self._closed.set()

    async def emit(self, event):
        handler = self.input_handler
        if handler is not None:
            await handler(event)


class _Backend:
    def __init__(self) -> None:
        self.sessions = []

    async def open(self, descriptor, frame):
        session = _BackendSession(frame)
        self.sessions.append(session)
        return session


@pytest.mark.asyncio
async def test_browser_window_releases_input_but_keeps_observing():
    backend = _Backend()
    registry = SurfacePresenterRegistry((BrowserWindowPresenter(backend),))
    surface = _Surface()
    presentation = await registry.present(surface)
    session = backend.sessions[0]
    try:
        await presentation.focus()
        await session.emit(SurfaceInput(kind="browser.text", data="hello"))
        assert surface.sent == [SurfaceInput(kind="browser.text", data="hello")]

        await presentation.synchronize()
        await presentation.release()
        assert session.input_handler is None
        assert session.closed is False

        session.updated.clear()
        surface.publish("agent update")
        await asyncio.wait_for(session.updated.wait(), timeout=1)
        assert "agent update" in session.frame.content
    finally:
        await registry.aclose()

    assert session.focused is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_browser_window_is_reused_and_user_close_only_detaches_observer():
    backend = _Backend()
    registry = SurfacePresenterRegistry((BrowserWindowPresenter(backend),))
    first_surface = _Surface("first")
    first = await registry.present(first_surface)
    await first.release()

    second_surface = _Surface("second")
    second = await registry.present(second_surface)
    assert second is first
    assert len(backend.sessions) == 1
    assert first_surface.detached is True
    assert backend.sessions[0].input_handler is not None

    await second.release()
    await backend.sessions[0].aclose()
    await asyncio.wait_for(second_surface.wait_detached(), timeout=1)

    third = await registry.present(_Surface("third"))
    try:
        assert third is not first
        assert len(backend.sessions) == 2
    finally:
        await registry.aclose()


@pytest.mark.asyncio
async def test_device_window_reuses_the_common_fenced_presentation_lifecycle():
    backend = _Backend()
    registry = SurfacePresenterRegistry((DeviceWindowPresenter(backend),))
    surface = _Surface(
        "device",
        kind="device",
        media_type="application/vnd.mote.device+json",
    )
    presentation = await registry.present(surface)
    try:
        assert isinstance(presentation, LiveWindowPresentationSession)
        await backend.sessions[0].emit(SurfaceInput(kind="device.key", data="BACK"))
        assert surface.sent == [SurfaceInput(kind="device.key", data="BACK")]

        await presentation.release()
        assert backend.sessions[0].input_handler is None
        backend.sessions[0].updated.clear()
        surface.publish("agent device update")
        await asyncio.wait_for(backend.sessions[0].updated.wait(), timeout=1)
        assert "agent device update" in backend.sessions[0].frame.content
    finally:
        await registry.aclose()


@pytest.mark.asyncio
async def test_jupyter_window_reuses_the_common_fenced_presentation_lifecycle():
    backend = _Backend()
    registry = SurfacePresenterRegistry((JupyterWindowPresenter(backend),))
    surface = _Surface(
        "notebook",
        kind="notebook",
        media_type=NOTEBOOK_MEDIA_TYPE,
    )
    presentation = await registry.present(surface)
    try:
        assert isinstance(presentation, LiveWindowPresentationSession)
        event = SurfaceInput(
            kind="notebook.execute",
            data=json.dumps({"cell_id": "cell-human", "source": "2 + 2"}),
        )
        await backend.sessions[0].emit(event)
        assert surface.sent == [event]

        await presentation.release()
        assert backend.sessions[0].input_handler is None
        backend.sessions[0].updated.clear()
        surface.publish("agent notebook update")
        await asyncio.wait_for(backend.sessions[0].updated.wait(), timeout=1)
        assert "agent notebook update" in backend.sessions[0].frame.content
    finally:
        await registry.aclose()


@pytest.mark.asyncio
async def test_terminal_window_reuses_the_common_fenced_presentation_lifecycle():
    backend = _Backend()
    registry = SurfacePresenterRegistry((TerminalWindowPresenter(backend),))
    surface = _Surface(
        "terminal",
        kind="terminal",
        media_type="text/x-terminal",
    )
    presentation = await registry.present(surface)
    try:
        event = SurfaceInput(kind="terminal.input", data="echo hello\r")
        await backend.sessions[0].emit(event)
        assert surface.sent == [event]
        await presentation.release()
        assert backend.sessions[0].input_handler is None

        backend.sessions[0].updated.clear()
        surface.publish("agent terminal update")
        await asyncio.wait_for(backend.sessions[0].updated.wait(), timeout=1)
        assert "agent terminal update" in backend.sessions[0].frame.content
    finally:
        await registry.aclose()
