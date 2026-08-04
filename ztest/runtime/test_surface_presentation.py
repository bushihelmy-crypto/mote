from __future__ import annotations

import asyncio

import pytest

from mote.contracts.ports.surface.canvas_backend import CanvasBackendCapabilities
from mote.contracts.surface import (
    CanvasDocument,
    CanvasEllipse,
    CanvasRectangle,
    SurfaceDescriptor,
    SurfaceFrame,
    SurfacePresentationMode,
)
from mote.product.interfaces.textual.surfaces.canvas import CanvasWindowPresenter
from mote.runtime.interactive.presentation import SurfacePresenterRegistry


class _Surface:
    descriptor = SurfaceDescriptor(
        kind="canvas",
        ref="canvas:test",
        presentation=SurfacePresentationMode.WINDOW,
    )

    def __init__(self) -> None:
        self.document = CanvasDocument(
            elements=[
                CanvasRectangle(
                    id="first",
                )
            ]
        )
        self.sequence = 1
        self.sent = []
        self.detached = False
        self._changed = asyncio.Event()
        self._detached = asyncio.Event()

    async def snapshot(self):
        return SurfaceFrame(
            sequence=self.sequence,
            media_type="application/vnd.mote.canvas+json",
            content=self.document.model_dump_json(),
        )

    async def send(self, event):
        self.sent.append(event)
        self.document = CanvasDocument.model_validate_json(event.data)
        self.sequence += 1
        self._changed.set()

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

    def publish(self, document):
        self.document = document
        self.sequence += 1
        self._changed.set()


class _BackendSession:
    def __init__(self, scene) -> None:
        self.scene = scene
        self.focused = False
        self.closed = False
        self.editable = False
        self._closed = asyncio.Event()
        self.updated = asyncio.Event()

    async def focus(self):
        self.focused = True

    async def set_human_editable(self, editable):
        self.editable = editable

    async def apply_delta(self, operations):
        return None

    async def replace_scene(self, scene):
        self.scene = scene
        self.updated.set()

    async def snapshot_scene(self):
        return self.scene.model_copy(update={"elements": [*self.scene.elements, CanvasEllipse(id="human")]})

    async def wait_closed(self):
        await self._closed.wait()

    async def render(self):
        raise NotImplementedError

    async def export(self, format):
        raise NotImplementedError

    async def aclose(self):
        self.closed = True
        self._closed.set()


class _Backend:
    name = "fake"
    capabilities = CanvasBackendCapabilities(native_window=True, human_editing=True, screenshots=True)

    def __init__(self) -> None:
        self.session = None
        self.sessions = []

    async def open(self, scene):
        self.session = _BackendSession(scene)
        self.sessions.append(self.session)
        return self.session


@pytest.mark.asyncio
async def test_window_presenter_registry_synchronizes_canonical_scene():
    backend = _Backend()
    registry = SurfacePresenterRegistry((CanvasWindowPresenter(backend),))
    surface = _Surface()

    presentation = await registry.present(surface)
    try:
        await presentation.focus()
        assert backend.session.editable is True
        await presentation.synchronize()
        await presentation.release()

        assert backend.session.closed is False
        assert backend.session.editable is False
        backend.session.updated.clear()
        surface.publish(
            surface.document.model_copy(update={"elements": [*surface.document.elements, CanvasRectangle(id="agent")]})
        )
        await asyncio.wait_for(backend.session.updated.wait(), timeout=1)
        assert [element.id for element in backend.session.scene.elements] == ["first", "human", "agent"]
    finally:
        await presentation.aclose()

    assert backend.session.focused is True
    assert backend.session.closed is True
    assert surface.sent[0].kind == "canvas.replace"
    document = CanvasDocument.model_validate_json(surface.sent[0].data)
    assert [element.id for element in document.elements] == ["first", "human"]


@pytest.mark.asyncio
async def test_registry_reuses_open_window_and_reopens_after_user_closes_it():
    backend = _Backend()
    registry = SurfacePresenterRegistry((CanvasWindowPresenter(backend),))
    first_surface = _Surface()
    first = await registry.present(first_surface)
    await first.release()

    second_surface = _Surface()
    second_surface.document = CanvasDocument(
        elements=[
            CanvasRectangle(
                id="second",
            )
        ]
    )
    second = await registry.present(second_surface)

    assert second is first
    assert len(backend.sessions) == 1
    assert first_surface.detached is True
    assert backend.session.editable is True

    await second.release()
    await backend.session.aclose()
    await asyncio.wait_for(second_surface.wait_detached(), timeout=1)
    third = await registry.present(_Surface())
    try:
        assert third is not first
        assert len(backend.sessions) == 2
    finally:
        await registry.aclose()
