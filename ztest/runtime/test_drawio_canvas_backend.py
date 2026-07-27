from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest

from mote.contracts.canvas import CanvasDocument, CanvasElement
from mote.runtime.tools.dependency.canvas_backends.drawio import (
    DrawioCanvasBackend,
    DrawioCanvasSession,
    _DrawioCell,
    _element_from_cell,
    _export_png_headless,
    _scene_file,
)


@pytest.mark.asyncio
async def test_read_only_mode_keeps_graph_enabled_for_browsing(monkeypatch):
    session = DrawioCanvasSession(CanvasDocument())
    calls = []

    async def evaluate(body, payload=None):
        calls.append((body, payload))
        return None

    monkeypatch.setattr(session, "_graph_eval", evaluate)

    await session.set_human_editable(False)
    await session.set_human_editable(True)

    assert [payload for _, payload in calls] == [False, True]
    assert all("graph.setEnabled(true)" in body for body, _ in calls)
    assert all("setCellsEditable" in body for body, _ in calls)


def test_drawio_file_maps_canonical_shapes_and_edges():
    scene = CanvasDocument(
        width=900,
        height=600,
        elements=[
            CanvasElement(id="node", kind="ellipse", x=20, y=30, width=120, height=80),
            CanvasElement(
                id="edge",
                kind="arrow",
                x=80,
                y=110,
                x2=400,
                y2=300,
                source_id="node",
            ),
        ],
    )

    root = ElementTree.fromstring(_scene_file(scene))
    cells = {cell.attrib["id"]: cell for cell in root.findall(".//mxCell") if "id" in cell.attrib}

    assert root.tag == "mxfile"
    assert "ellipse" in cells["node"].attrib["style"]
    assert cells["edge"].attrib["edge"] == "1"
    assert cells["edge"].attrib["source"] == "node"
    assert cells["edge"].find("./mxGeometry/mxPoint[@as='sourcePoint']") is not None


def test_drawio_file_auto_sizes_unbounded_text_and_uses_fill_as_font_color():
    scene = CanvasDocument(
        elements=[
            CanvasElement(
                id="label",
                kind="text",
                x=30,
                y=40,
                text="中文 Canvas 标签",
                style={"fill": "#123456", "stroke": "none", "font_size": 20},
            )
        ]
    )

    root = ElementTree.fromstring(_scene_file(scene))
    cell = root.find(".//mxCell[@id='label']")

    assert cell is not None
    assert "fontColor=#123456" in cell.attrib["style"]
    assert "whiteSpace=nowrap" in cell.attrib["style"]
    geometry = cell.find("./mxGeometry")
    assert geometry is not None
    assert float(geometry.attrib["width"]) > 0
    assert float(geometry.attrib["height"]) > 0


def test_drawio_file_preserves_explicit_text_box_wrapping():
    scene = CanvasDocument(
        elements=[
            CanvasElement(
                id="label",
                kind="text",
                text="wrapped label",
                width=180,
                height=60,
            )
        ]
    )

    root = ElementTree.fromstring(_scene_file(scene))
    cell = root.find(".//mxCell[@id='label']")

    assert cell is not None
    assert "whiteSpace=wrap" in cell.attrib["style"]
    geometry = cell.find("./mxGeometry")
    assert geometry is not None
    assert geometry.attrib["width"] == "180.0"
    assert geometry.attrib["height"] == "60.0"


def test_drawio_cell_round_trip_preserves_native_extension_and_stable_id():
    cell = _DrawioCell(
        id="human shape/1",
        kind="vertex",
        label="Native",
        x=10,
        y=20,
        width=100,
        height=60,
        x2=0,
        y2=0,
        style="shape=hexagon;fillColor=#ffffff;strokeColor=#000000;fontSize=16;",
        source="",
        target="",
    )

    first = _element_from_cell(cell)
    second = _element_from_cell(cell)

    assert first.id == second.id
    assert first.id.startswith("drawio:")
    assert first.extensions["org.diagrams.net/cell@1"]["style"] == cell.style


@pytest.mark.asyncio
async def test_drawio_document_export_is_deterministic_and_opens_no_session():
    scene = CanvasDocument(elements=[CanvasElement(id="box", kind="rect")])
    backend = DrawioCanvasBackend(executable="unused")

    first = await backend.export(scene, "drawio")
    second = await backend.export(scene, ".drawio")

    assert first.content == second.content
    assert first.content == _scene_file(scene).encode("utf-8")


@pytest.mark.asyncio
async def test_png_export_uses_one_shot_cli_without_presenter(monkeypatch):
    observed = {}

    class Process:
        def __init__(self, spec):
            observed["argv"] = spec.argv

        async def start(self):
            Path(observed["argv"][5]).write_bytes(b"\x89PNG\r\n\x1a\nimage")

        async def wait(self):
            return 0

        async def aclose(self):
            observed["closed"] = True

    monkeypatch.setattr(
        "mote.runtime.tools.dependency.canvas_backends.drawio.ManagedExternalProcess",
        Process,
    )

    content = await _export_png_headless(b"<mxfile/>", executable="drawio")

    assert content.startswith(b"\x89PNG")
    assert observed["argv"][1:5] == ("--export", "--format", "png", "--output")
    assert observed["closed"] is True
