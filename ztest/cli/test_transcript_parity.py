#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TranscriptReducer`` parity — the "run-twice" golden that proves both hosts agree.

One :func:`_script` of :class:`ViewEvent`\\ s covers every timing tension: a
streamed reply, a reasoning stream, a Read/Search group, a NONE tool + file
diff, a DETAIL (Bash) tool, a retry that a following delta self-clears, a
transparent usage update, an error, and a compaction boundary.

* :func:`test_reducer_golden` feeds the script to **one** :class:`TranscriptReducer`
  and snapshots the emitted ``list[TranscriptOp]`` to an inline golden — the single
  host-independent source of truth that both rich hosts decode identically.
* :func:`test_terminal_landing` runs the script through the scrolling
  ``SurfaceDriver(TerminalSurface)`` and asserts the rich output (grouped line,
  expanded Bash, in-place compaction marker, converged non-persistent reasoning).
* :func:`test_textual_landing` runs the same script through ``MoteApp`` under a
  pilot and asserts the widget tree (one group widget, thinking toggles, the
  compaction clear + bridge recap).
"""

from __future__ import annotations

import io
from typing import Any, List, Tuple

import pytest

from mote.product.cli.contracts.view import (
    ConversationCompacted,
    ErrorRaised,
    FileDiffBlock,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    ReasoningDelta,
    RetryStatus,
    ToolCallCompleted,
    ToolCallStarted,
    UsageUpdated,
)
from mote.product.i18n import keys as K
from mote.product.i18n import t


def _script() -> List[Any]:
    """The shared event sequence — fresh instances per call (events are reused)."""
    return [
        MessageBlockCompleted(role="user", markdown="do stuff"),
        MessageBlockStarted(role="assistant"),
        MessageBlockDelta(text="Hello "),
        MessageBlockDelta(text="world"),
        MessageBlockCompleted(role="assistant", markdown="Hello world", streamed=True),
        ReasoningDelta(text="pondering"),
        ToolCallStarted(tool_name="Read", headline="a.py", tool_use_id="t1"),
        ToolCallCompleted(tool_name="Read", ok=True, summary="42 lines", tool_use_id="t1"),
        ToolCallStarted(tool_name="Search", headline="foo", tool_use_id="t2"),
        ToolCallCompleted(tool_name="Search", ok=True, summary="3 matches", tool_use_id="t2"),
        ToolCallStarted(tool_name="Search", headline="*.py", tool_use_id="t3"),
        ToolCallCompleted(tool_name="Search", ok=True, summary="5 files", tool_use_id="t3"),
        ToolCallStarted(tool_name="Edit", headline="a.py", tool_use_id="t4"),
        ToolCallCompleted(tool_name="Edit", ok=True, summary="1 change", tool_use_id="t4"),
        FileDiffBlock(path="a.py", old="x = 1\n", new="x = 2\n"),
        ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id="t5"),
        ToolCallCompleted(tool_name="Bash", ok=True, summary="ok", tool_use_id="t5"),
        RetryStatus(attempt=1, max_attempts=3, delay_ms=1000.0, error_type="LLMOverloadedError"),
        MessageBlockDelta(text="answer"),
        UsageUpdated(),
        ErrorRaised(text="boom"),
        ConversationCompacted(summary="recap", message_count=4),
    ]


def _key(op: Any) -> Tuple[Any, ...]:
    """Collapse an op to its distinguishing semantic fields (host-independent)."""
    k = op.kind
    if k == "open_block":
        return (k, op.role)
    if k == "append_delta":
        return (k, op.text, op.reasoning)
    if k == "close_block":
        return (k, op.markdown, op.streamed)
    if k == "render_user_message":
        return (k, op.markdown)
    if k in ("tool_started", "tool_completed"):
        return (k, getattr(op.ev, "tool_name", ""), op.fold.value)
    if k in ("add_to_group", "complete_in_group"):
        return (k, getattr(op.ev, "tool_name", ""))
    if k == "render_file_diff":
        return (k, getattr(op.ev, "path", ""))
    if k == "render_error":
        return (k, getattr(op.ev, "text", ""))
    if k == "set_thinking":
        return (k, op.on)
    if k == "clear_for_compaction":
        return (k, op.summary, op.message_count, op.last_user_prompt)
    return (k,)


# The single host-independent truth: the exact op stream both hosts decode.
_GOLDEN: List[Tuple[Any, ...]] = [
    ("render_user_message", "do stuff"),
    ("open_block", "assistant"),
    ("append_delta", "Hello ", False),
    ("append_delta", "world", False),
    ("close_block", "Hello world", True),
    ("set_thinking", True),
    ("append_delta", "pondering", True),
    ("set_thinking", False),
    ("open_group",),
    ("add_to_group", "Read"),
    ("complete_in_group", "Read"),
    ("add_to_group", "Search"),
    ("complete_in_group", "Search"),
    ("add_to_group", "Search"),
    ("complete_in_group", "Search"),
    ("flush_group",),
    ("tool_started", "Edit", "none"),
    ("tool_completed", "Edit", "none"),
    ("render_file_diff", "a.py"),
    ("tool_started", "Bash", "detail"),
    ("tool_completed", "Bash", "detail"),
    ("set_retry",),
    ("clear_retry",),
    ("append_delta", "answer", False),
    ("update_usage",),
    ("render_error", "boom"),
    ("clear_for_compaction", "recap", 4, "do stuff"),
]


def test_reducer_golden():
    """Feed the script to one reducer; the op stream matches the inline golden.

    This is the machine proof that "when to merge a run / clear a transient / end
    thinking / wipe on compaction" is decided **once**, host-blind — so the two
    surfaces below can only ever render the same decisions.
    """
    from mote.product.cli.consumers.transcript import TranscriptReducer

    reducer = TranscriptReducer()
    ops: List[Tuple[Any, ...]] = []
    for ev in _script():
        ops.extend(_key(op) for op in reducer.feed(ev))
    assert ops == _GOLDEN


# --------------------------------------------------------------------------
# Terminal landing — the scrolling SurfaceDriver(TerminalSurface)
# --------------------------------------------------------------------------

try:
    import rich  # noqa: F401

    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False


@pytest.mark.skipif(not _HAS_RICH, reason="rich required")
def test_terminal_landing():
    from rich.console import Console

    from mote.product.cli.consumers.terminal.surface import TerminalSurface
    from mote.product.cli.consumers.transcript import SurfaceDriver

    console = Console(file=io.StringIO(), force_terminal=True, width=120)
    driver = SurfaceDriver(TerminalSurface(console=console))
    for ev in _script():
        driver.on_unhandled(ev)
    out = console.file.getvalue()

    # The Read/Search run collapsed into one summary line (2 searches, 1 read).
    assert t(K.GROUP_SEARCH, count=2) in out
    assert t(K.GROUP_READ, count=1) in out
    # The DETAIL Bash tool renders expanded (headline visible) on the linear host.
    assert "Bash" in out and "ls" in out
    # Compaction prints an in-place ✻ boundary (no wipe — scrollback survives).
    from mote.product.cli.consumers.render.palette import COMPACT

    assert COMPACT in out
    assert "boom" in out  # the error surfaced
    # Converged behaviour: reasoning tokens never enter the permanent scrollback.
    assert "pondering" not in out


# --------------------------------------------------------------------------
# Textual landing — the full-screen MoteApp under a pilot
# --------------------------------------------------------------------------

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_textual_landing():
    from mote.product.cli.consumers.textual.app import MoteApp, ViewEventMessage
    from mote.product.cli.consumers.textual.widgets import (
        CompactionSummaryRow,
        ConversationCompactedRow,
        StatusBar,
        ToolCallWidget,
        ToolGroupWidget,
        UserMessageRow,
    )

    script = _script()
    app = MoteApp()
    async with app.run_test() as pilot:

        def post(ev: Any) -> None:
            app.post_message(ViewEventMessage(ev))

        # Up to the reasoning delta (index 5) → the thinking flag is set.
        for ev in script[:6]:
            post(ev)
        await pilot.pause()
        bar = app.query_one("#status", StatusBar)
        assert bar.thinking is True

        # The first grouped tool start ends thinking and opens one group widget.
        for ev in script[6:15]:  # through the file diff (before Bash)
            post(ev)
        await pilot.pause()
        assert bar.thinking is False
        assert len(app.query(ToolGroupWidget)) == 1

        # The DETAIL Bash tool mounts a collapsed ToolCallWidget (fold-by-default).
        for ev in script[15:21]:  # Bash start/complete .. error (pre-compaction)
            post(ev)
        await pilot.pause()
        # Both the NONE Edit and DETAIL Bash tools mount collapsed (fold-by-default).
        assert app.query(ToolCallWidget)
        assert all(not w.expanded for w in app.query(ToolCallWidget))

        # Compaction wipes the stale rows and re-renders only the bridge.
        post(script[21])
        await pilot.pause()
        assert len(app.query(ToolGroupWidget)) == 0
        assert len(app.query(ToolCallWidget)) == 0
        assert len(app.query(ConversationCompactedRow)) == 1
        assert len(app.query(CompactionSummaryRow)) == 1
        assert len(app.query(UserMessageRow)) == 1
        assert app._last_user_prompt == "do stuff"
