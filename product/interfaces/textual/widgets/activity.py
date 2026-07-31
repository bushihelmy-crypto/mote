#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``ActivityWidget`` — the full-screen host's live view of a nested orchestration.

A ``run_graph`` call (and every future nested orchestrator — a sub-agent, a
background task) opens ONE :class:`ActivityWidget` keyed by its ``scope``. Unlike
the append-only terminal (which prints the topology once and can't retro-edit
scrolled rows), a Textual widget re-renders wholesale on every mutation, so this
widget lights nodes up **live**: the reducer's ``update_activity_node`` /
``add_activity_tool_call`` / ``complete_activity_tool_call`` ops feed per-node
status + folded child tool-call rows, and each re-renders the widget in place.

While the activity runs it shows the declared topology (``activity_topology``)
plus a live per-node status trail and the folded child tool calls. When the
reducer's ``close_activity`` op lands it freezes to the **self-sufficient**
outcome tree (``activity_outcome``) read straight off the terminal event's
``node_states`` — the same block the terminal prints — so a replayed / resumed
transcript renders identically without the live stream.

Modelled on :class:`~mote.product.interfaces.textual.widgets.transcript.ToolGroupWidget`:
a :class:`FoldableRow` whose ``_rebuild`` reads its own accumulated state, folding
its detail (the child rows) under ``ctrl+o`` while the one-line header stays.
"""

from __future__ import annotations

from typing import Any, List, Optional

from rich.console import Group
from rich.text import Text

from mote.product.interfaces.textual.style import BRANCH, Palette
from mote.product.interfaces.textual.widgets.transcript import FoldableRow, build_tool_parts
from mote.product.presentation.rich_rendering.builders import activity_header, activity_outcome, activity_topology

# Per-node live status → colour for the running trail (a subset of the outcome
# tree's mapping; the terminal outcome builder owns the final glyph set).
_LIVE_STATUS_STYLE = {
    "running": Palette.BRAND,
    "success": Palette.SUCCESS,
    "failed": Palette.ERROR,
    "skipped": Palette.DIM,
    "cancelled": Palette.WARNING,
}


class ActivityWidget(FoldableRow):
    """One nested orchestration, keyed by ``scope`` — live topology → outcome."""

    def __init__(self, activity_kind: str, label: str, topology: Any, *, expanded: bool = True, **kwargs: Any) -> None:
        # Default expanded: a running orchestration must stay readable (its child
        # rows + live node trail visible); ctrl+o folds it to the header afterwards.
        super().__init__(expanded=expanded, **kwargs)
        self._activity_kind = activity_kind
        self._label = label
        self._topology = topology
        # Live per-node status keyed by the ping's ``stage`` (the node id/name):
        # ``{stage: (status, detail)}`` — most-recent value wins, rendered as a
        # dim trail below the topology while the activity runs.
        self._node_status: dict[str, tuple[str, str]] = {}
        # Folded child tool calls dispatched inside the activity: mutable
        # ``[started_event, completed_event | None]`` pairs (graph-internal calls
        # carry ``tool_use_id=None``, so completion correlates positionally).
        self._children: List[list] = []
        # Terminal state, set on close — freezes the render to the outcome tree.
        # NB: MUST NOT be named ``_closed`` — that shadows Textual's
        # ``MessagePump._closed``, whose truthiness forces ``display=False`` (the
        # widget vanishes the instant the run completes).
        self._frozen = False
        self._outcome = "success"
        self._node_states: Any = ()
        self._summary = ""
        self._rebuild()

    # -- live mutation (mirrors ToolGroupWidget.add_started / complete) --
    def update_node(self, stage: str, status: str, detail: str) -> None:
        if stage:
            self._node_status[stage] = (status or "", detail or "")
        self._rebuild()

    def add_child(self, ev: Any) -> None:
        self._children.append([ev, None])
        self._rebuild()

    def complete_child(self, ev: Any) -> None:
        tid = getattr(ev, "tool_use_id", None)
        # Correlate by tool_use_id when present; graph-internal calls have none,
        # so fall back to the first uncompleted entry with the same tool name.
        target: Optional[list] = None
        if tid:
            for entry in self._children:
                if getattr(entry[0], "tool_use_id", None) == tid:
                    target = entry
                    break
        if target is None:
            name = getattr(ev, "tool_name", None)
            for entry in self._children:
                if entry[1] is None and getattr(entry[0], "tool_name", None) == name:
                    target = entry
                    break
        if target is None:
            for entry in self._children:
                if entry[1] is None:
                    target = entry
                    break
        if target is not None:
            target[1] = ev
        self._rebuild()

    def finalize_outcome(self, outcome: str, node_states: Any, summary: str) -> None:
        self._frozen = True
        self._outcome = outcome or "success"
        self._node_states = node_states or ()
        self._summary = summary or ""
        self._rebuild()

    # -- render --
    def _live_trail(self) -> Optional[Text]:
        """The dim per-node status trail shown while the activity runs."""
        if not self._node_status:
            return None
        text = Text()
        first = True
        for stage, (status, detail) in self._node_status.items():
            if not first:
                text.append("\n")
            first = False
            text.append(f"    {BRANCH} ", style=Palette.DIM)
            text.append(stage, style=_LIVE_STATUS_STYLE.get(status, Palette.BRAND))
            if status:
                text.append(f" · {status}", style=Palette.DIM)
            if detail:
                text.append(f" · {detail}", style=Palette.DIM)
        return text

    def _rebuild(self) -> None:
        if self._frozen:
            # Frozen: the self-sufficient outcome tree (identical to the terminal).
            self.update(activity_outcome(self._node_states, self._outcome, self._summary))
            return
        if not self.expanded:
            # Folded: just the ``● label (kind)`` header — child detail hidden.
            self.update(activity_header(self._activity_kind, self._label))
            return
        parts: list[Any] = [activity_topology(self._activity_kind, self._label, self._topology)]
        trail = self._live_trail()
        if trail is not None:
            parts.append(trail)
        for started, completed in self._children:
            parts.extend(build_tool_parts(started, completed))
        self.update(Group(*parts))


__all__ = ["ActivityWidget"]
