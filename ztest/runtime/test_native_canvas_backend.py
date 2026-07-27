from __future__ import annotations

import pytest

from mote.contracts.canvas import CanvasDocument, CanvasElement
from mote.contracts.ports.canvas_backend import CanvasBackendSession
from mote.runtime.tools.dependency.canvas_backends.native import NativeCanvasBackend


@pytest.mark.asyncio
async def test_native_canvas_backend_exports_svg_without_window():
    session = await NativeCanvasBackend().open(
        CanvasDocument(
            elements=[
                CanvasElement(
                    id="headless-box",
                    kind="rect",
                    width=120,
                    height=80,
                )
            ]
        )
    )
    try:
        assert isinstance(session, CanvasBackendSession)
        rendered = await session.render()
        exported = await session.export("svg")

        assert rendered.mime_type == "image/svg+xml"
        assert exported.representation == "svg"
        assert exported.mime_type == rendered.mime_type
        assert exported.content == rendered.content
        assert b'<rect id="headless-box"' in exported.content
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_native_canvas_backend_rejects_unsupported_export():
    session = await NativeCanvasBackend().open(CanvasDocument())
    try:
        with pytest.raises(ValueError, match="cannot export"):
            await session.export("png")
    finally:
        await session.aclose()
