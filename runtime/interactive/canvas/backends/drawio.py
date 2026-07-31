"""Visible diagrams.net Desktop backend driven through its native graph API."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import socket
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from mote.contracts.ports.surface.canvas_backend import CanvasBackendCapabilities, CanvasBackendRender
from mote.contracts.surface import (
    CanvasDocument,
    CanvasElement,
    CanvasExportRepresentation,
    CanvasOperation,
    CanvasStyle,
)
from mote.runtime.interactive.canvas.state import apply_canvas_operations
from mote.runtime.interactive.cdp import CdpBrowserConnection
from mote.runtime.interactive.process import ExternalProcessSpec, ManagedExternalProcess

_DRAWIO_EXTENSION = "org.diagrams.net/model@1"
_DRAWIO_CELL_EXTENSION = "org.diagrams.net/cell@1"
_DRAWIO_TARGET = re.compile(r"draw\.io|diagrams\.net", re.IGNORECASE)
_CANONICAL_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
_COLOR = re.compile(r"^(?:#[0-9a-fA-F]{3,8}|[a-zA-Z]+|none)$")
_GRAPH_LOOKUP = """
const findUi = () => {
  for (const key of ['ui', 'editorUi', 'app']) {
    try { if (window[key]?.editor?.graph?.getModel) return window[key]; } catch (_) {}
  }
  for (const key of Object.keys(window)) {
    try { if (window[key]?.editor?.graph?.getModel) return window[key]; } catch (_) {}
  }
  return null;
};
const ui = findUi();
const graph = window.__moteDrawioGraph || ui?.editor?.graph;
if (!graph) throw new Error('draw.io graph is not ready');
"""


class DrawioBackendUnavailableError(RuntimeError):
    """draw.io Desktop is absent or its native graph API cannot be reached."""


@dataclass(frozen=True, slots=True)
class _DrawioCell:
    id: str
    kind: str
    label: str
    x: float
    y: float
    width: float
    height: float
    x2: float
    y2: float
    style: str
    source: str
    target: str


class DrawioCanvasBackend:
    """Open canonical Canvas scenes in the full diagrams.net Desktop editor."""

    name = "drawio"
    capabilities = CanvasBackendCapabilities(
        native_window=True,
        human_editing=True,
        screenshots=True,
        import_formats=frozenset({"drawio"}),
        export_formats=frozenset({"drawio", "png"}),
    )

    def __init__(self, executable: str = "") -> None:
        self._executable = executable

    async def open(self, scene: CanvasDocument) -> "DrawioCanvasSession":
        session = DrawioCanvasSession(scene, executable=self._executable)
        await session.start()
        return session

    async def export(
        self,
        scene: CanvasDocument,
        format: str,
    ) -> CanvasExportRepresentation:
        normalized = format.lower().lstrip(".")
        drawio = _scene_file(scene).encode("utf-8")
        if normalized == "drawio":
            return CanvasExportRepresentation(
                representation="drawio",
                mime_type="application/vnd.jgraph.mxfile",
                content=drawio,
                suggested_name="canvas.drawio",
            )
        if normalized != "png":
            raise ValueError(f"draw.io backend cannot export {format!r}")
        content = await _export_png_headless(
            drawio,
            executable=_resolve_executable(self._executable),
        )
        return CanvasExportRepresentation(
            representation="png",
            mime_type="image/png",
            content=content,
            suggested_name="canvas.png",
        )


class DrawioCanvasSession:
    """One owned draw.io window synchronized with a canonical Canvas scene."""

    def __init__(self, scene: CanvasDocument, *, executable: str = "") -> None:
        self._scene = scene.model_copy(deep=True)
        self._executable = executable
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self._process: ManagedExternalProcess | None = None
        self._connection = CdpBrowserConnection()
        self._page: Any = None
        self._cdp: Any = None
        self._window_closed = asyncio.Event()

    @property
    def closed(self) -> bool:
        process = self._process
        page = self._page
        return process is None or not process.health().running or page is None or page.is_closed()

    async def start(self) -> None:
        executable = _resolve_executable(self._executable)
        self._temp = tempfile.TemporaryDirectory(prefix="mote-drawio-")
        root = Path(self._temp.name)
        document_path = root / "canvas.drawio"
        document_path.write_text(_scene_file(self._scene), encoding="utf-8")
        port = _available_port()
        argv = (
            executable,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={root / 'profile'}",
            "--disable-features=CalculateNativeWinOcclusion",
            str(document_path),
        )
        self._process = ManagedExternalProcess(ExternalProcessSpec(argv=argv))
        try:
            await self._process.start()
            await self._connection.connect(f"http://127.0.0.1:{port}")
            self._page = await self._connection.find_page(_DRAWIO_TARGET)
            self._page.once("close", lambda *_: self._window_closed.set())
            self._cdp = await self._page.context.new_cdp_session(self._page)
            await self._wait_for_graph()
            await self._synchronize_scene(self._scene)
            await self.set_human_editable(False)
        except Exception as exc:
            await self.aclose()
            if isinstance(exc, DrawioBackendUnavailableError):
                raise
            raise DrawioBackendUnavailableError(f"draw.io window failed to start: {exc}") from exc

    async def focus(self) -> None:
        if self._page is None:
            raise RuntimeError("draw.io session is not open")
        await self._connection.focus(self._page, maximize=False)

    async def set_human_editable(self, editable: bool) -> None:
        await self._graph_eval(
            """
const controls = [
  ['setCellsEditable', 'cellsEditable'],
  ['setCellsMovable', 'cellsMovable'],
  ['setCellsResizable', 'cellsResizable'],
  ['setCellsBendable', 'cellsBendable'],
  ['setCellsCloneable', 'cellsCloneable'],
  ['setCellsDeletable', 'cellsDeletable'],
  ['setCellsDisconnectable', 'cellsDisconnectable'],
  ['setConnectable', 'connectable'],
  ['setDropEnabled', 'dropEnabled'],
  ['setSplitEnabled', 'splitEnabled'],
];
if (payload) {
  const state = window.__moteDrawioEditState;
  if (state) {
    for (const [setter, property] of controls) {
      if (typeof graph[setter] === 'function' && property in state) {
        graph[setter](state[property]);
      }
    }
    delete window.__moteDrawioEditState;
  }
} else if (!window.__moteDrawioEditState) {
  const state = {};
  for (const [setter, property] of controls) {
    if (typeof graph[setter] === 'function') {
      state[property] = Boolean(graph[property]);
      graph[setter](false);
    }
  }
  window.__moteDrawioEditState = state;
}
graph.setEnabled(true);
return {enabled: graph.isEnabled(), editable: Boolean(payload)};
""",
            editable,
        )

    async def apply_delta(self, operations: tuple[CanvasOperation, ...]) -> None:
        candidate, _, _ = apply_canvas_operations(self._scene, operations)
        payload: list[dict[str, Any]] = []
        for operation in operations:
            if operation.op == "clear":
                payload.append({"op": "clear"})
            elif operation.op == "remove":
                payload.append({"op": "remove", "id": operation.element_id})
            else:
                assert operation.element is not None
                payload.append({"op": "upsert", "element": _element_payload(operation.element)})
        try:
            await self._apply_payload(payload)
        except Exception:
            await self._synchronize_scene(self._scene)
            raise
        self._scene = candidate

    async def replace_scene(self, scene: CanvasDocument) -> None:
        await self._synchronize_scene(scene)
        self._scene = scene.model_copy(deep=True)

    async def snapshot_scene(self) -> CanvasDocument:
        result = await self._graph_eval(
            """
const model = graph.getModel();
const parent = graph.getDefaultParent();
const cells = graph.getChildCells(parent, true, true).map((cell) => {
  const geo = cell.geometry;
  const center = (terminal) => terminal?.geometry
    ? {x: Number(terminal.geometry.x) + Number(terminal.geometry.width) / 2,
       y: Number(terminal.geometry.y) + Number(terminal.geometry.height) / 2}
    : null;
  const source = center(cell.source) || geo?.sourcePoint || {x: 0, y: 0};
  const target = center(cell.target) || geo?.targetPoint || {x: 0, y: 0};
  return {
    id: String(cell.id), kind: cell.edge ? 'edge' : 'vertex',
    label: graph.convertValueToString(cell), style: cell.style || '',
    source: cell.source?.id || '', target: cell.target?.id || '',
    x: Number(geo?.x || 0), y: Number(geo?.y || 0),
    width: Number(geo?.width || 0), height: Number(geo?.height || 0),
    x2: Number(target.x || 0), y2: Number(target.y || 0),
    edgeX: Number(source.x || 0), edgeY: Number(source.y || 0),
  };
});
const codec = new mxCodec();
const modelXml = mxUtils.getXml(codec.encode(model));
return {cells, modelXml};
"""
        )
        cells = [_cell_from_raw(item) for item in result["cells"]]
        elements = [_element_from_cell(cell) for cell in cells]
        extensions = dict(self._scene.extensions)
        extensions[_DRAWIO_EXTENSION] = {
            "model_xml": result["modelXml"],
            "canonical_ids": [cell.id for cell in cells],
            "schema": 1,
        }
        self._scene = CanvasDocument.model_validate(
            self._scene.model_copy(update={"elements": elements, "extensions": extensions}).model_dump(mode="json")
        )
        return self._scene.model_copy(deep=True)

    async def wait_closed(self) -> None:
        if self.closed:
            return
        assert self._process is not None
        process_wait = asyncio.create_task(self._process.wait())
        window_wait = asyncio.create_task(self._window_closed.wait())
        tasks = (process_wait, window_wait)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def render(self) -> CanvasBackendRender:
        if self._page is None:
            raise RuntimeError("draw.io session is not open")
        return CanvasBackendRender(mime_type="image/png", content=await self._page.screenshot())

    async def export(self, format: str) -> CanvasExportRepresentation:
        normalized = format.lower().lstrip(".")
        if normalized == "png":
            rendered = await self.render()
            return CanvasExportRepresentation(
                representation="png",
                mime_type=rendered.mime_type,
                content=rendered.content,
                suggested_name="canvas.png",
            )
        if normalized != "drawio":
            raise ValueError(f"draw.io backend cannot export {format!r}")
        scene = await self.snapshot_scene()
        return CanvasExportRepresentation(
            representation="drawio",
            mime_type="application/vnd.jgraph.mxfile",
            content=_scene_file(scene).encode("utf-8"),
            suggested_name="canvas.drawio",
        )

    async def aclose(self) -> None:
        cdp, process, temp = self._cdp, self._process, self._temp
        self._cdp = None
        self._page = None
        self._process = None
        self._temp = None
        self._window_closed.set()
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:  # noqa: BLE001 - target may already be closed
                pass
        try:
            await self._connection.aclose()
        finally:
            if process is not None:
                await process.aclose()
            if temp is not None:
                temp.cleanup()

    async def _wait_for_graph(self, timeout_seconds: float = 25.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self._recover_graph_reference() and await self._page.evaluate(
                    "!!(window.__moteDrawioGraph?.getModel)"
                ):
                    return
            except Exception:  # noqa: BLE001 - draw.io initializes in stages
                pass
            await asyncio.sleep(0.3)
        raise DrawioBackendUnavailableError("draw.io opened but its graph API did not become ready")

    async def _recover_graph_reference(self) -> bool:
        existing = await self._cdp.send(
            "Runtime.evaluate",
            {
                "expression": "!!(window.__moteDrawioGraph?.getModel && window.__moteDrawioGraph?.insertVertex)",
                "returnByValue": True,
            },
        )
        if existing.get("result", {}).get("value") is True:
            return True
        listeners = await self._cdp.send(
            "Runtime.evaluate",
            {
                "expression": "document.querySelector('.geDiagramContainer')?.mxListenerList?.map(x => x.f).filter(Boolean) || []",
                "returnByValue": False,
            },
        )
        list_id = listeners.get("result", {}).get("objectId")
        if not list_id:
            return False
        properties = await self._properties(list_id)
        functions = [
            item
            for item in properties.get("result", [])
            if item.get("name", "").isdigit() and item.get("value", {}).get("objectId")
        ][:40]
        for function in functions:
            details = await self._properties(function["value"]["objectId"], own=False)
            scopes_id = next(
                (
                    item.get("value", {}).get("objectId")
                    for item in details.get("internalProperties", [])
                    if item.get("name") == "[[Scopes]]"
                ),
                None,
            )
            if not scopes_id:
                continue
            scopes = await self._properties(scopes_id)
            for scope_item in scopes.get("result", []):
                remote = scope_item.get("value", {})
                if not scope_item.get("name", "").isdigit() or not remote.get("objectId"):
                    continue
                if "Closure" not in remote.get("description", ""):
                    continue
                scope = await self._properties(remote["objectId"])
                for variable in scope.get("result", []):
                    value = variable.get("value", {})
                    object_id = value.get("objectId")
                    if not object_id or value.get("type") != "object":
                        continue
                    if value.get("className") == "Graph" and await self._bind_graph(object_id):
                        return True
                    if value.get("className") not in {
                        "mxCellEditor",
                        "EditorUi",
                        "Editor",
                        "Graph",
                        "Object",
                    }:
                        continue
                    nested = await self._properties(object_id)
                    graph = next(
                        (
                            item.get("value", {})
                            for item in nested.get("result", [])
                            if item.get("name") == "graph" and item.get("value", {}).get("objectId")
                        ),
                        None,
                    )
                    if graph and await self._bind_graph(graph["objectId"]):
                        return True
        return False

    async def _properties(self, object_id: str, *, own: bool = True) -> dict[str, Any]:
        return await self._cdp.send(
            "Runtime.getProperties",
            {
                "objectId": object_id,
                "ownProperties": own,
                "accessorPropertiesOnly": False,
                "generatePreview": True,
            },
        )

    async def _bind_graph(self, object_id: str) -> bool:
        result = await self._cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": "function(){window.__moteDrawioGraph=this;return !!(this?.getModel&&this?.insertVertex)}",
                "returnByValue": True,
                "userGesture": True,
            },
        )
        return result.get("result", {}).get("value") is True

    async def _graph_eval(self, body: str, payload: Any = None) -> Any:
        if self._page is None:
            raise RuntimeError("draw.io session is not open")
        return await self._page.evaluate(f"(payload) => {{ {_GRAPH_LOOKUP} {body} }}", payload)

    async def _synchronize_scene(self, scene: CanvasDocument) -> None:
        extension = scene.extensions.get(_DRAWIO_EXTENSION)
        previous = extension.get("canonical_ids", []) if isinstance(extension, dict) else []
        current = {_raw_cell_id(element) for element in scene.elements}
        operations = [{"op": "remove", "id": cell_id} for cell_id in previous if cell_id not in current]
        operations.extend({"op": "upsert", "element": _element_payload(element)} for element in scene.elements)
        await self._apply_payload(operations)
        await self._graph_eval("graph.fit(20, false, 20, true, false, true); return true;")

    async def _apply_payload(self, operations: list[dict[str, Any]]) -> None:
        await self._graph_eval(
            """
const model = graph.getModel();
const parent = graph.getDefaultParent();
model.beginUpdate();
try {
  for (const operation of payload) {
    if (operation.op === 'clear') {
      graph.removeCells(graph.getChildCells(parent, true, true), true);
      continue;
    }
    if (operation.op === 'remove') {
      const cell = model.getCell(operation.id);
      if (cell) graph.removeCells([cell], true);
      continue;
    }
    const element = operation.element;
    let cell = model.getCell(element.id);
    const wantsEdge = element.kind === 'edge';
    if (cell && Boolean(cell.edge) !== wantsEdge) {
      graph.removeCells([cell], true);
      cell = null;
    }
    const source = element.source ? model.getCell(element.source) : null;
    const target = element.target ? model.getCell(element.target) : null;
    if (!cell && wantsEdge) cell = graph.insertEdge(parent, element.id, element.label, source, target, element.style);
    if (!cell) cell = graph.insertVertex(parent, element.id, element.label, element.x, element.y, element.width, element.height, element.style);
    graph.cellLabelChanged(cell, element.label, false);
    model.setStyle(cell, element.style);
    const geometry = cell.geometry?.clone() || new mxGeometry();
    if (wantsEdge) {
      model.setTerminal(cell, source, true);
      model.setTerminal(cell, target, false);
      geometry.relative = true;
      geometry.sourcePoint = new mxPoint(element.x, element.y);
      geometry.targetPoint = new mxPoint(element.x2, element.y2);
    } else {
      geometry.x = element.x; geometry.y = element.y;
      geometry.width = element.width; geometry.height = element.height;
    }
    model.setGeometry(cell, geometry);
  }
} finally { model.endUpdate(); }
graph.clearSelection();
return true;
""",
            operations,
        )


def _resolve_executable(configured: str) -> str:
    explicit = configured or os.environ.get("DRAWIO_PATH", "").strip()
    candidates: list[str] = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates.extend(
                    (
                        str(Path(base) / "draw.io" / "draw.io.exe"),
                        str(Path(base) / "Programs" / "draw.io" / "draw.io.exe"),
                    )
                )
    elif sys.platform == "darwin":
        candidates.extend(
            (
                "/Applications/draw.io.app/Contents/MacOS/draw.io",
                str(Path.home() / "Applications/draw.io.app/Contents/MacOS/draw.io"),
            )
        )
    else:
        candidates.extend(
            (
                "/usr/bin/drawio",
                "/usr/local/bin/drawio",
                "/snap/bin/drawio",
                "/opt/drawio/drawio",
            )
        )
    path_candidate = shutil.which("drawio") or shutil.which("draw.io.exe")
    if path_candidate:
        candidates.append(path_candidate)
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise DrawioBackendUnavailableError(
        "draw.io Desktop is required for Canvas integration; install it or set DRAWIO_PATH"
    )


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _export_png_headless(
    drawio: bytes,
    *,
    executable: str,
    timeout_seconds: float = 60.0,
) -> bytes:
    """Run diagrams.net's one-shot CLI exporter without a presenter session."""
    with tempfile.TemporaryDirectory(prefix="mote-drawio-export-") as temp:
        root = Path(temp)
        source = root / "canvas.drawio"
        output = root / "canvas.png"
        source.write_bytes(drawio)
        process = ManagedExternalProcess(
            ExternalProcessSpec(
                argv=(
                    executable,
                    "--export",
                    "--format",
                    "png",
                    "--output",
                    str(output),
                    str(source),
                )
            )
        )
        await process.start()
        try:
            return_code = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise DrawioBackendUnavailableError("draw.io headless PNG export timed out") from exc
        finally:
            await process.aclose()
        if return_code != 0 or not output.is_file():
            raise DrawioBackendUnavailableError(f"draw.io headless PNG export failed with exit code {return_code}")
        content = output.read_bytes()
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise DrawioBackendUnavailableError("draw.io headless export did not produce a valid PNG")
        return content


def _scene_file(scene: CanvasDocument) -> str:
    extension = scene.extensions.get(_DRAWIO_EXTENSION)
    model_xml = extension.get("model_xml") if isinstance(extension, dict) else None
    if not isinstance(model_xml, str) or not _valid_model_xml(model_xml):
        model_xml = _model_xml(scene)
    else:
        model_xml = ElementTree.tostring(ElementTree.fromstring(model_xml), encoding="unicode")
    modified = "1970-01-01T00:00:00.000Z"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<mxfile host="Electron" modified="{modified}" version="mote">\n'
        f'  <diagram id="mote-canvas" name="Canvas">\n{model_xml}\n  </diagram>\n</mxfile>\n'
    )


def _valid_model_xml(value: str) -> bool:
    try:
        return ElementTree.fromstring(value).tag == "mxGraphModel"
    except ElementTree.ParseError:
        return False


def _model_xml(scene: CanvasDocument) -> str:
    model = ElementTree.Element(
        "mxGraphModel",
        page="1",
        pageWidth=str(scene.width),
        pageHeight=str(scene.height),
        background=scene.background,
    )
    root = ElementTree.SubElement(model, "root")
    ElementTree.SubElement(root, "mxCell", id="0")
    ElementTree.SubElement(root, "mxCell", id="1", parent="0")
    for element in scene.elements:
        payload = _element_payload(element)
        attrs = {
            "id": payload["id"],
            "value": payload["label"],
            "style": payload["style"],
            "parent": "1",
        }
        if payload["kind"] == "edge":
            attrs["edge"] = "1"
            if payload["source"]:
                attrs["source"] = payload["source"]
            if payload["target"]:
                attrs["target"] = payload["target"]
            cell = ElementTree.SubElement(root, "mxCell", attrs)
            geometry = ElementTree.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
            ElementTree.SubElement(
                geometry,
                "mxPoint",
                x=str(payload["x"]),
                y=str(payload["y"]),
                **{"as": "sourcePoint"},
            )
            ElementTree.SubElement(
                geometry,
                "mxPoint",
                x=str(payload["x2"]),
                y=str(payload["y2"]),
                **{"as": "targetPoint"},
            )
        else:
            attrs["vertex"] = "1"
            cell = ElementTree.SubElement(root, "mxCell", attrs)
            ElementTree.SubElement(
                cell,
                "mxGeometry",
                x=str(payload["x"]),
                y=str(payload["y"]),
                width=str(payload["width"]),
                height=str(payload["height"]),
                **{"as": "geometry"},
            )
    return ElementTree.tostring(model, encoding="unicode")


def _raw_cell_id(element: CanvasElement) -> str:
    extension = element.extensions.get(_DRAWIO_CELL_EXTENSION)
    raw = extension.get("id") if isinstance(extension, dict) else None
    return raw if isinstance(raw, str) and raw else element.id


def _element_payload(element: CanvasElement) -> dict[str, Any]:
    style = element.style
    width = element.width
    height = element.height
    common = f"fillColor={style.fill};strokeColor={style.stroke};strokeWidth={style.stroke_width};fontSize={style.font_size};whiteSpace=wrap;html=1;"
    if element.kind == "ellipse":
        drawio_style = "ellipse;" + common
    elif element.kind == "text":
        wrap = width > 0
        width, height = _text_geometry(element)
        font_color = style.fill if style.fill != "none" else style.stroke
        drawio_style = (
            "text;strokeColor=none;fillColor=none;align=left;verticalAlign=top;"
            f"fontColor={font_color};fontSize={style.font_size};"
            f"whiteSpace={'wrap' if wrap else 'nowrap'};html=1;"
        )
    elif element.kind in {"line", "arrow"}:
        arrow = "none" if element.kind == "line" else "block"
        drawio_style = f"endArrow={arrow};endFill=1;html=1;strokeColor={style.stroke};strokeWidth={style.stroke_width};"
    else:
        drawio_style = "rounded=0;" + common
    extension = element.extensions.get(_DRAWIO_CELL_EXTENSION)
    source = element.source_id
    target = element.target_id
    if isinstance(extension, dict):
        raw_style = extension.get("style")
        if isinstance(raw_style, str) and raw_style:
            drawio_style = _merge_style(raw_style, drawio_style)
        if not source and isinstance(extension.get("source"), str):
            source = extension["source"]
        if not target and isinstance(extension.get("target"), str):
            target = extension["target"]
    return {
        "id": _raw_cell_id(element),
        "kind": "edge" if element.kind in {"line", "arrow"} else "vertex",
        "label": element.text,
        "x": element.x,
        "y": element.y,
        "width": width,
        "height": height,
        "x2": element.x2,
        "y2": element.y2,
        "source": source,
        "target": target,
        "style": drawio_style,
    }


def _text_geometry(element: CanvasElement) -> tuple[float, float]:
    lines = element.text.splitlines() or [""]
    width = element.width
    if width <= 0:
        units = max(sum(_text_character_width(char) for char in line) for line in lines)
        width = max(element.style.font_size, units * element.style.font_size) + 8.0
    height = element.height
    if height <= 0:
        height = len(lines) * element.style.font_size * 1.25 + 4.0
    return width, height


def _text_character_width(char: str) -> float:
    if char.isspace():
        return 0.35
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 1.0
    return 0.6


def _cell_from_raw(raw: dict[str, Any]) -> _DrawioCell:
    return _DrawioCell(
        id=str(raw["id"]),
        kind=str(raw["kind"]),
        label=str(raw.get("label", "")),
        x=float(raw.get("edgeX", raw.get("x", 0))),
        y=float(raw.get("edgeY", raw.get("y", 0))),
        width=max(0.0, float(raw.get("width", 0))),
        height=max(0.0, float(raw.get("height", 0))),
        x2=float(raw.get("x2", 0)),
        y2=float(raw.get("y2", 0)),
        style=str(raw.get("style", "")),
        source=str(raw.get("source", "")),
        target=str(raw.get("target", "")),
    )


def _element_from_cell(cell: _DrawioCell) -> CanvasElement:
    style_map = _style_map(cell.style)
    if cell.kind == "edge":
        kind = "line" if style_map.get("endArrow") in {None, "none"} else "arrow"
    elif "ellipse" in style_map or style_map.get("shape") in {"ellipse", "cloud"}:
        kind = "ellipse"
    elif "text" in style_map:
        kind = "text"
    else:
        kind = "rect"
    canonical_id = _canonical_cell_id(cell.id)
    text_color = style_map.get("fontColor") if kind == "text" else style_map.get("strokeColor")
    return CanvasElement(
        id=canonical_id,
        kind=kind,
        x=cell.x,
        y=cell.y,
        width=cell.width,
        height=cell.height,
        x2=cell.x2,
        y2=cell.y2,
        text=cell.label,
        style=CanvasStyle(
            fill=_valid_color(style_map.get("fillColor"), "none"),
            stroke=_valid_color(text_color, "#7aa2f7"),
            stroke_width=_bounded_number(style_map.get("strokeWidth"), 2.0, 0.0, 64.0),
            font_size=_bounded_number(style_map.get("fontSize"), 24.0, 4.0, 256.0),
        ),
        source_id=cell.source if _CANONICAL_ID.fullmatch(cell.source) else "",
        target_id=cell.target if _CANONICAL_ID.fullmatch(cell.target) else "",
        extensions={
            _DRAWIO_CELL_EXTENSION: {
                "id": cell.id,
                "style": cell.style,
                "source": cell.source,
                "target": cell.target,
            }
        },
    )


def _style_map(style: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for token in style.split(";"):
        if not token:
            continue
        if "=" in token:
            key, value = token.split("=", 1)
            result[key] = value
        else:
            result[token] = "1"
    return result


def _merge_style(base: str, overlay: str) -> str:
    merged = _style_map(base)
    merged.update(_style_map(overlay))
    return ";".join(key if value == "1" else f"{key}={value}" for key, value in merged.items()) + ";"


def _canonical_cell_id(raw_id: str) -> str:
    if _CANONICAL_ID.fullmatch(raw_id):
        return raw_id
    return f"drawio:{hashlib.sha256(raw_id.encode('utf-8')).hexdigest()[:24]}"


def _valid_color(value: str | None, fallback: str) -> str:
    return value if value and _COLOR.fullmatch(value) else fallback


def _bounded_number(value: str | None, fallback: float, lower: float, upper: float) -> float:
    try:
        number = float(value) if value is not None else fallback
    except ValueError:
        number = fallback
    return min(max(number, lower), upper)


__all__ = [
    "DrawioBackendUnavailableError",
    "DrawioCanvasBackend",
    "DrawioCanvasSession",
]
