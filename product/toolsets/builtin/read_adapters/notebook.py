"""Jupyter Notebook-to-text adaptation."""

from __future__ import annotations

import json


class NotebookDecodeError(Exception):
    """Notebook bytes are not valid UTF-8 text."""


class NotebookJsonError(Exception):
    """Notebook text is not valid JSON."""


def _cell_source(cell: dict) -> str:
    source = cell.get("source", "")
    if isinstance(source, list):
        return "".join(source)
    return source or ""


def _render_outputs(cell: dict) -> list[str]:
    rendered: list[str] = []
    for output in cell.get("outputs", []) or []:
        output_type = output.get("output_type")
        if output_type == "stream":
            text = output.get("text", "")
            rendered.append("".join(text) if isinstance(text, list) else text)
        elif output_type in ("execute_result", "display_data"):
            data = output.get("data", {}) or {}
            text = data.get("text/plain", "")
            rendered.append("".join(text) if isinstance(text, list) else text)
            if "image/png" in data or "image/jpeg" in data:
                rendered.append("[image output omitted]")
        elif output_type == "error":
            traceback = output.get("traceback", []) or []
            rendered.append("\n".join(traceback) if isinstance(traceback, list) else str(traceback))
    return [item for item in rendered if item]


def render_notebook(notebook: dict) -> str:
    """Flatten parsed Notebook cells and textual outputs into readable text."""
    parts: list[str] = []
    for index, cell in enumerate(notebook.get("cells", []) or [], start=1):
        cell_type = cell.get("cell_type", "code")
        source = _cell_source(cell)
        parts.append(f"# ── Cell {index} [{cell_type}] ──")
        if source:
            parts.append(source.rstrip("\n"))
        if cell_type == "code":
            outputs = _render_outputs(cell)
            if outputs:
                parts.append("# Output:")
                parts.append("\n".join(output.rstrip("\n") for output in outputs))
    return "\n".join(parts)


def parse_notebook(raw: bytes) -> str:
    """Decode, parse, and flatten Notebook bytes."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotebookDecodeError(str(exc)) from exc
    try:
        notebook = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotebookJsonError(str(exc)) from exc
    return render_notebook(notebook)


__all__ = ["NotebookDecodeError", "NotebookJsonError", "parse_notebook"]
