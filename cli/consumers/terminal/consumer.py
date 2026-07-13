#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Terminal hosts for the human ``ViewEvent`` protocol.

The rich scrolling host is assembled from two host-agnostic parts: the
:class:`~mote.cli.consumers.transcript.reducer.TranscriptReducer` (the single
event-orchestration state machine, shared with the Textual app) and the
:class:`~mote.cli.consumers.terminal.surface.TerminalSurface` (the rich
rendering primitives — the incremental-Markdown live region, the bullet+branch
layout, the transient retry/thinking regions, the three-tier image renderer).
:func:`build_terminal_consumer` wires them behind a
:class:`~mote.cli.consumers.transcript.driver.SurfaceDriver`, itself an ordinary
``BaseConsumer`` — so the projector / ``CapabilityAdapter`` path is untouched.

``rich`` is an optional dependency. When absent, :func:`build_terminal_consumer`
returns :class:`PlainTerminalConsumer`, a rich-less fallback that degrades every
event to a simple ``print`` — the app keeps working without color (§9.10). The
plain consumer stays a *plain* ``BaseConsumer`` (it opts out of the reducer):
with no erasable region it buffers whole blocks and has no grouping/thinking
timing to share.
"""

from __future__ import annotations

import sys
from typing import Any

from mote.cli.consumers.render.builders import file_change_verb
from mote.cli.consumers.render.builders import fold_note_str as _fold_note_str
from mote.cli.consumers.render.builders import format_usage_line as _format_usage_line
from mote.cli.consumers.render.palette import COMPACT, NOTE
from mote.cli.contracts.base import BaseConsumer
from mote.cli.contracts.view import TERMINAL_CAPS, Capabilities
from mote.common.i18n import keys as K
from mote.common.i18n import t

try:  # rich is optional; degrade to plain text when absent.
    import rich  # noqa: F401

    _HAS_RICH = True
except ImportError:  # pragma: no cover — exercised via the plain-text fallback
    _HAS_RICH = False


class PlainTerminalConsumer(BaseConsumer):
    """Plain-text fallback when ``rich`` is unavailable — no color, no live region.

    Declares ``streaming=False`` so the upstream :class:`CapabilityAdapter`
    buffers deltas into a single ``MessageBlockCompleted`` and this consumer only
    ever prints whole blocks (avoids token-by-token print spam).
    """

    capabilities: Capabilities = Capabilities(streaming=False, markdown=False)

    def __init__(self, out=None):
        self._out = out if out is not None else sys.stdout

    def _print(self, text: str) -> None:
        self._out.write(text + "\n")

    def on_message_block_completed(self, ev: Any) -> None:
        if ev.role == "user":
            if ev.markdown.strip():
                self._print(f"> {ev.markdown}")
            return
        if ev.markdown.strip():
            self._print(ev.markdown)
        self._show_truncation(ev)

    def _show_truncation(self, ev: Any) -> None:
        if not getattr(ev, "content_truncated", False):
            return
        self._print("  " + _fold_note_str(ev))

    def on_tool_call_started(self, ev: Any) -> None:
        head = f"  {ev.headline}" if ev.headline else ""
        self._print(f"[{ev.tool_name}]{head}")
        if ev.body:
            self._print(ev.body)

    def on_tool_call_completed(self, ev: Any) -> None:
        mark = "✓" if ev.ok else "✗"
        self._print(f"  {mark} {ev.summary or t(K.RESULT_NO_OUTPUT)}")
        self._show_truncation(ev)

    def on_media_block(self, ev: Any) -> None:
        label = ev.media_kind or "media"
        ref = ev.ref or ev.alt or "(no reference)"
        self._print(f"  [{label}] {ref}")

    def on_file_diff_block(self, ev: Any) -> None:
        old = getattr(ev, "old", "") or ""
        new = getattr(ev, "new", "") or ""
        path = getattr(ev, "path", "") or ""
        self._print(f"  [{file_change_verb(old, new)}] {path or 'file'}")

    def on_approval_requested(self, ev: Any) -> None:
        action = ev.action or ev.tool_name or "action"
        self._print(f"  {t(K.APPROVAL_REQUIRED)} [{ev.risk}]: {action}")

    def on_usage_updated(self, ev: Any) -> None:
        line = _format_usage_line(ev)
        if line:
            self._print("  · " + line)

    def on_task_progress(self, ev: Any) -> None:
        self._print(f"  {ev.stage} {ev.status}{(': ' + ev.detail) if ev.detail else ''}")

    def on_notice(self, ev: Any) -> None:
        self._print(ev.text)

    def on_system_reminder(self, ev: Any) -> None:
        self._print(f"{NOTE} {getattr(ev, 'text', '') or ''}")

    def on_conversation_compacted(self, ev: Any) -> None:
        count = getattr(ev, "message_count", 0) or 0
        tail = f" ({t(K.COMPACT_KEPT, count=count)})" if count else ""
        self._print(f"{COMPACT} " + t(K.COMPACT_COMPACTED) + tail)

    def on_retry_status(self, ev: Any) -> None:
        # No erasable region in plain mode — silently swallow the transient
        # retry countdown (printing it would violate "never persist a retry
        # line"). A genuinely exhausted retry still surfaces as a final error.
        return None

    def on_error_raised(self, ev: Any) -> None:
        self._print(f"Error: {ev.text}")

    def on_question_asked(self, ev: Any) -> None:
        self._print(f"? {ev.question}")

    def on_session_list_shown(self, ev: Any) -> None:
        if not ev.items:
            self._print("(no sessions)")
            return
        self._print(ev.title)
        for item in ev.items:
            label = item.label or item.session_id
            updated = f"  {item.updated_at}" if item.updated_at else ""
            preview = f"  {item.preview}" if item.preview else ""
            self._print(f"  {item.index}. {label}{updated}{preview}")

    def on_transcript_cleared(self, ev: Any) -> None:
        # No scrollback to wipe in plain mode; just note the reset.
        self._print("(conversation cleared)")


def build_terminal_consumer(config: Any = None):
    """Return the rich scrolling consumer, or a plain-text one if rich is absent."""
    if _HAS_RICH:
        from mote.cli.consumers.terminal.surface import TerminalSurface
        from mote.cli.consumers.transcript import SurfaceDriver

        return SurfaceDriver(TerminalSurface())
    return PlainTerminalConsumer()


# Self-register on import (the registry imports this module).
try:
    from mote.cli.consumers.registry import register_consumer

    register_consumer("terminal", capabilities=TERMINAL_CAPS)(build_terminal_consumer)
except Exception:  # noqa: BLE001 — registry optional during isolated import/tests
    pass


__all__ = ["PlainTerminalConsumer", "build_terminal_consumer"]
