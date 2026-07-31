from __future__ import annotations

import asyncio
import os

import pytest

from mote.contracts.interaction.handoff import HandoffRequest, HandoffStatus, HumanHandoffOutcome
from mote.contracts.runtime import RuntimeRef
from mote.contracts.surface import CanvasDocument, CanvasElement, CanvasOperation
from mote.product.interfaces.textual.surfaces.canvas import CanvasWindowPresentationSession
from mote.runtime.interactive.canvas.backends.drawio import DrawioCanvasBackend
from mote.runtime.interactive.canvas.driver import CanvasRuntimeDriver
from mote.runtime.interactive.surface import RuntimeLiveSurfaceSession

pytestmark = pytest.mark.skipif(
    os.environ.get("MOTE_RUN_DRAWIO_LIVE") != "1",
    reason="set MOTE_RUN_DRAWIO_LIVE=1 to open the real draw.io Desktop window",
)


@pytest.mark.asyncio
async def test_real_drawio_window_round_trip():
    backend = DrawioCanvasBackend()
    session = await backend.open(
        CanvasDocument(
            width=800,
            height=600,
            elements=[
                CanvasElement(
                    id="node-a",
                    kind="rect",
                    x=80,
                    y=100,
                    width=180,
                    height=90,
                    text="Agent",
                ),
                CanvasElement(
                    id="node-b",
                    kind="ellipse",
                    x=500,
                    y=300,
                    width=180,
                    height=90,
                    text="User",
                ),
                CanvasElement(
                    id="edge-a-b",
                    kind="arrow",
                    x=260,
                    y=145,
                    x2=500,
                    y2=345,
                    source_id="node-a",
                    target_id="node-b",
                ),
            ],
        )
    )
    try:
        await session._graph_eval(
            """
const parent = graph.getDefaultParent();
graph.getModel().beginUpdate();
try {
  graph.insertVertex(parent, 'human-live', 'Human edit', 300, 420, 200, 80,
    'rounded=1;fillColor=#d5e8d4;strokeColor=#82b366;');
} finally { graph.getModel().endUpdate(); }
return true;
"""
        )
        scene = await session.snapshot_scene()
        rendered = await session.render()
        exported = await session.export("drawio")

        assert {element.id for element in scene.elements} >= {
            "node-a",
            "node-b",
            "edge-a-b",
            "human-live",
        }
        assert rendered.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert b"<mxfile" in exported.content
        assert b"human-live" in exported.content
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_drawio_window_observes_agent_edits_after_handoff_returns():
    driver = CanvasRuntimeDriver(CanvasDocument(elements=[CanvasElement(id="before", kind="rect", text="Before")]))
    await driver.start()
    handle = await driver.prepare_handoff(
        HandoffRequest(runtime_ref=RuntimeRef(runtime_id="canvas-live-observer", kind="canvas"))
    )
    surface = RuntimeLiveSurfaceSession(driver, handle)
    backend = await DrawioCanvasBackend().open(driver.snapshot_document())
    presentation = CanvasWindowPresentationSession(backend)
    try:
        await presentation.attach(surface)
        assert await backend._graph_eval("return graph.isEnabled();") is True

        await backend._graph_eval(
            """
const parent = graph.getDefaultParent();
graph.getModel().beginUpdate();
try {
  graph.insertVertex(parent, 'human-live', 'Human', 300, 300, 160, 80,
    'rounded=1;fillColor=#d5e8d4;strokeColor=#82b366;');
} finally { graph.getModel().endUpdate(); }
return true;
"""
        )
        await presentation.synchronize()
        await presentation.release()
        await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.COMPLETED))

        assert await backend._graph_eval("return graph.isEnabled();") is True
        assert await backend._graph_eval("return graph.cellsEditable;") is False
        driver.apply(
            [
                CanvasOperation(
                    op="upsert",
                    element=CanvasElement(id="agent-live", kind="ellipse", x=600, y=200, text="Agent"),
                )
            ]
        )
        for _ in range(50):
            if await backend._graph_eval("return Boolean(graph.getModel().getCell('agent-live'));"):
                break
            await asyncio.sleep(0.1)
        assert await backend._graph_eval("return Boolean(graph.getModel().getCell('agent-live'));") is True
        assert presentation.closed is False
    finally:
        await presentation.aclose()
        await driver.aclose()
