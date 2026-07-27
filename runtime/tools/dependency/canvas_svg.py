"""Deterministic pure SVG projection of canonical Canvas documents."""
from __future__ import annotations

import html

from mote.contracts.canvas import CanvasDocument, CanvasElement


def render_canvas_svg(document: CanvasDocument) -> str:
    body = "\n".join(_element_svg(element) for element in document.elements)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{document.width}" '
        f'height="{document.height}" viewBox="0 0 {document.width} {document.height}">\n'
        '<defs><marker id="arrow" markerWidth="10" markerHeight="7" refX="9" '
        'refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" '
        'fill="context-stroke"/></marker></defs>\n'
        f'<rect width="100%" height="100%" '
        f'fill="{html.escape(document.background, quote=True)}"/>\n'
        f"{body}\n</svg>"
    )


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _element_svg(element: CanvasElement) -> str:
    style = element.style
    attrs = (
        f'fill="{html.escape(style.fill, quote=True)}" '
        f'stroke="{html.escape(style.stroke, quote=True)}" '
        f'stroke-width="{_fmt(style.stroke_width)}"'
    )
    if element.kind == "rect":
        return (
            f'<rect id="{element.id}" x="{_fmt(element.x)}" y="{_fmt(element.y)}" '
            f'width="{_fmt(element.width)}" height="{_fmt(element.height)}" {attrs}/>'
        )
    if element.kind == "ellipse":
        return (
            f'<ellipse id="{element.id}" cx="{_fmt(element.x + element.width / 2)}" '
            f'cy="{_fmt(element.y + element.height / 2)}" '
            f'rx="{_fmt(element.width / 2)}" ry="{_fmt(element.height / 2)}" {attrs}/>'
        )
    if element.kind in {"line", "arrow"}:
        marker = ' marker-end="url(#arrow)"' if element.kind == "arrow" else ""
        return (
            f'<line id="{element.id}" x1="{_fmt(element.x)}" y1="{_fmt(element.y)}" '
            f'x2="{_fmt(element.x2)}" y2="{_fmt(element.y2)}" {attrs}{marker}/>'
        )
    return (
        f'<text id="{element.id}" x="{_fmt(element.x)}" y="{_fmt(element.y)}" '
        f'fill="{html.escape(style.fill if style.fill != "none" else style.stroke, quote=True)}" '
        f'font-size="{_fmt(style.font_size)}">{html.escape(element.text)}</text>'
    )


__all__ = ["render_canvas_svg"]
