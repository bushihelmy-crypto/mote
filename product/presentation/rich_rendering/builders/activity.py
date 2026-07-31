"""Nested orchestration activity renderables."""

from __future__ import annotations

import json
from typing import Any

from mote.product.presentation.rich_rendering.builders._rich import Text
from mote.product.presentation.rich_rendering.palette import BRANCH, CHECK, CROSS, PLAY, SKIP, Palette

_NODE_KIND_GLYPH = {"tool": "◆", "map": "⇉", "fold": "→", "compute": "ƒ"}
_NODE_STATUS_STYLE = {
    "success": (CHECK, Palette.SUCCESS),
    "failed": (CROSS, Palette.ERROR),
    "skipped": (SKIP, Palette.DIM),
    "cancelled": (SKIP, Palette.WARNING),
    "running": (PLAY, Palette.BRAND),
    "pending": (SKIP, Palette.DIM),
}


def activity_header(activity_kind: str, label: str) -> "Text":
    line = Text()
    line.append("  " + BRANCH + " ", style=Palette.DIM)
    line.append(label or activity_kind or "activity", style=f"bold {Palette.BRAND}")
    if activity_kind and activity_kind != (label or ""):
        line.append(f" ({activity_kind})", style=Palette.DIM)
    return line


def activity_topology(activity_kind: str, label: str, topology: Any) -> "Text":
    text = activity_header(activity_kind, label)
    topology = topology or {}
    for node in topology.get("nodes") or []:
        node_id = node.get("id", "") or ""
        kind = node.get("kind", "") or ""
        text.append("\n")
        text.append(f"    {_NODE_KIND_GLYPH.get(kind, '•')} ", style=Palette.DIM)
        text.append(node.get("label", "") or node_id, style=Palette.BRAND)
        if kind:
            text.append(f" [{kind}]", style=Palette.DIM)
    for edge in (edge for edge in topology.get("edges") or [] if edge.get("guarded")):
        text.append("\n")
        text.append(f"      {BRANCH} ", style=Palette.DIM)
        text.append(f"{edge.get('from', '?')} → {edge.get('to', '?')}", style=Palette.DIM)
        text.append(" (when)", style=Palette.DIM)
    return text


def _node_retry_args(node: Any) -> str:
    args = node.get("args")
    if not args:
        return ""
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(args)
    return text[:200] + "…" if len(text) > 200 else text


def activity_outcome(node_states: Any, outcome: str, summary: str) -> "Text":
    line = Text()
    ok = (outcome or "success") == "success"
    glyph, style = (CHECK, Palette.SUCCESS) if ok else (CROSS, Palette.ERROR)
    line.append("  " + BRANCH + " ", style=Palette.DIM)
    line.append(f"{glyph} {outcome or 'success'}", style=style)
    for node in node_states or ():
        status = node.get("status", "") or ""
        glyph, style = _NODE_STATUS_STYLE.get(status, (SKIP, Palette.DIM))
        line.append("\n")
        line.append(f"    {glyph} ", style=style)
        line.append(node.get("label", "") or node.get("id", "") or "", style=style)
        attempts = int(node.get("attempts", 0) or 0)
        if attempts > 1:
            line.append(f" (×{attempts})", style=Palette.DIM)
        if status == "failed":
            if node.get("error"):
                line.append(f": {node['error']}", style=Palette.DIM)
            retry_args = _node_retry_args(node)
            if retry_args:
                line.append("\n")
                line.append(f"      {BRANCH} {retry_args}", style=Palette.DIM)
    if summary and summary.strip():
        line.append("\n")
        line.append(f"    {summary.strip()}", style=Palette.DIM)
    return line


__all__ = ["activity_header", "activity_outcome", "activity_topology"]
