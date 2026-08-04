"""Nested orchestration activity renderables."""

from __future__ import annotations

import json

from mote.contracts.activity import (
    ActivityKind,
    ActivityNodeKind,
    ActivityNodeState,
    ActivityNodeStatus,
    ActivityOutcome,
    ActivityTopology,
)
from mote.product.presentation.rich_rendering.builders._rich import Text
from mote.product.presentation.rich_rendering.palette import BRANCH, CHECK, CROSS, PLAY, SKIP, Palette

_NODE_KIND_GLYPH = {
    ActivityNodeKind.TOOL: "◆",
    ActivityNodeKind.MAP: "⇉",
    ActivityNodeKind.FOLD: "→",
    ActivityNodeKind.COMPUTE: "ƒ",
}
_NODE_STATUS_STYLE = {
    ActivityNodeStatus.SUCCESS: (CHECK, Palette.SUCCESS),
    ActivityNodeStatus.FAILED: (CROSS, Palette.ERROR),
    ActivityNodeStatus.SKIPPED: (SKIP, Palette.DIM),
    ActivityNodeStatus.CANCELLED: (SKIP, Palette.WARNING),
    ActivityNodeStatus.RUNNING: (PLAY, Palette.BRAND),
    ActivityNodeStatus.PENDING: (SKIP, Palette.DIM),
}


def activity_header(activity_kind: ActivityKind, label: str) -> "Text":
    line = Text()
    line.append("  " + BRANCH + " ", style=Palette.DIM)
    line.append(label or activity_kind or "activity", style=f"bold {Palette.BRAND}")
    if activity_kind and activity_kind != (label or ""):
        line.append(f" ({activity_kind})", style=Palette.DIM)
    return line


def activity_topology(
    activity_kind: ActivityKind,
    label: str,
    topology: ActivityTopology | None,
) -> "Text":
    text = activity_header(activity_kind, label)
    topology = topology or ActivityTopology((), ())
    for node in topology.nodes:
        node_id = node.node_id
        kind = node.kind
        text.append("\n")
        text.append(f"    {_NODE_KIND_GLYPH.get(kind, '•')} ", style=Palette.DIM)
        text.append(node.label or node_id, style=Palette.BRAND)
        if kind is not ActivityNodeKind.UNSPECIFIED:
            text.append(f" [{kind.value}]", style=Palette.DIM)
    for edge in (edge for edge in topology.edges if edge.guarded):
        text.append("\n")
        text.append(f"      {BRANCH} ", style=Palette.DIM)
        text.append(f"{edge.from_node} → {edge.to_node}", style=Palette.DIM)
        text.append(" (when)", style=Palette.DIM)
    return text


def _node_retry_args(node: ActivityNodeState) -> str:
    args = node.arguments
    if not args:
        return ""
    try:
        text = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(args)
    return text[:200] + "…" if len(text) > 200 else text


def activity_outcome(
    node_states: tuple[ActivityNodeState, ...],
    outcome: ActivityOutcome,
    summary: str,
) -> "Text":
    line = Text()
    ok = outcome is ActivityOutcome.SUCCESS
    glyph, style = (CHECK, Palette.SUCCESS) if ok else (CROSS, Palette.ERROR)
    line.append("  " + BRANCH + " ", style=Palette.DIM)
    line.append(f"{glyph} {outcome.value}", style=style)
    for node in node_states:
        status = node.status
        glyph, style = _NODE_STATUS_STYLE.get(status, (SKIP, Palette.DIM))
        line.append("\n")
        line.append(f"    {glyph} ", style=style)
        line.append(node.label or node.node_id, style=style)
        attempts = node.attempts
        if attempts > 1:
            line.append(f" (×{attempts})", style=Palette.DIM)
        if status is ActivityNodeStatus.FAILED:
            if node.error:
                line.append(f": {node.error}", style=Palette.DIM)
            retry_args = _node_retry_args(node)
            if retry_args:
                line.append("\n")
                line.append(f"      {BRANCH} {retry_args}", style=Palette.DIM)
    if summary and summary.strip():
        line.append("\n")
        line.append(f"    {summary.strip()}", style=Palette.DIM)
    return line


__all__ = ["activity_header", "activity_outcome", "activity_topology"]
