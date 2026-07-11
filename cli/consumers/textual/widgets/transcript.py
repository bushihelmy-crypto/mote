#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Transcript-area Textual widgets.

Every widget here is a thin :class:`SelectableStatic` that mounts into the
scrolling transcript. The **rich renderables** they display are produced by the
SHARED ``mote.cli.consumers.render.builders`` — the exact same diff colours,
TSV tables, tool headline/summary text the rich terminal draws — so the two
hosts never diverge on look (§9.7 "format once").

Textual re-renders a widget wholesale on every ``update`` / reactive change, so
the incremental-``Live`` streaming machinery from the terminal consumer has no
analogue here: :class:`AssistantBlock` simply re-renders its accumulated
markdown on each delta.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from rich.console import Group
from rich.text import Text

from mote.cli.consumers.render.builders import (
    RESULT_INDENT,
    bullet_row,
    compaction_summary_text,
    conversation_compacted_text,
    file_change_caption,
    indent,
    linkify,
    media_caption,
    notice_style,
    render_file_change,
    render_image,
    render_result_detail,
    task_progress_text,
    tool_body_syntax,
    tool_completed_text,
    tool_group_summary_text,
    tool_started_text,
    user_message_row,
)
from mote.cli.consumers.render.markdown import themed_markdown
from mote.cli.consumers.textual.style import BULLET, NOTE, WARN, Palette
from mote.cli.consumers.textual.widgets.base import SelectableStatic


class AssistantBlock(SelectableStatic):
    """A streaming assistant (or reasoning) markdown block.

    Accumulates deltas in ``_buf`` and re-renders the whole markdown on each
    ``append_delta`` — Textual handles the incremental repaint, so there is no
    need for the terminal consumer's block-boundary ``Live`` region.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._buf = ""

    def append_delta(self, text: str) -> None:
        if not text:
            return
        self._buf += text
        self._rebuild()

    def set_markdown(self, markdown: str) -> None:
        """Replace the buffer wholesale (non-streamed completed block)."""
        self._buf = markdown or ""
        self._rebuild()

    def finalize(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        if self._buf.strip():
            self.update(bullet_row(BULLET, themed_markdown(self._buf), style=Palette.BRAND))


def build_tool_parts(started: Any, completed: Any, *, blink: bool = False) -> list:
    """Assemble the renderable parts for one tool call (started + optional completed).

    Shared by :class:`ToolCallWidget` (a standalone transcript row) and
    :class:`ToolGroupWidget` (the expanded view of a collapsed search/read run) so
    the two never diverge on layout: the ``● Tool(headline)`` invocation line, an
    optional highlighted body, the result summary line and its structured detail
    (diff / table / linkified plain preview). The bullet is coloured by run state
    (brand while running, green/red on completion); ``blink`` pulses the running
    bullet (the host toggles it each heartbeat).
    """
    ok = None if completed is None else bool(getattr(completed, "ok", True))
    parts: list[Any] = [tool_started_text(started, ok=ok, blink=blink and completed is None)]
    body = tool_body_syntax(started)
    if body is not None:
        parts.append(indent(body, RESULT_INDENT))
    if completed is not None:
        parts.append(tool_completed_text(completed))
        # The shared builder renders the structured detail per kind (diff/table/
        # plain) so this host never diverges from the rich terminal; a plain
        # preview's bare URLs stay linkified for Ctrl+click.
        parts.extend(render_result_detail(completed, RESULT_INDENT))
    return parts


class ToolCallWidget(SelectableStatic):
    """One tool invocation + its result, keyed by ``tool_use_id``.

    Built from the ``ToolCallStarted`` event; :meth:`complete` folds in the
    matching ``ToolCallCompleted`` (correlated by ``tool_use_id`` in the app) so
    the started line, optional body, result summary and structured detail
    (diff / table) render together as one transcript row.
    """

    #: The running bullet pulses at this cadence (secs) until the call completes.
    _BLINK_INTERVAL = 0.5

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tool_use_id: Optional[str] = getattr(ev, "tool_use_id", None)
        self._started = ev
        self._completed: Any = None
        self._blink = False
        self._blink_timer: Any = None
        self._rebuild()

    def on_mount(self) -> None:
        # Pulse the running bullet until the result lands (claude-code's blink);
        # a call that already completed before mount never starts the timer.
        if self._completed is None:
            self._blink_timer = self.set_interval(self._BLINK_INTERVAL, self._pulse)

    def _pulse(self) -> None:
        if self._completed is not None:
            return
        self._blink = not self._blink
        self._rebuild()

    def complete(self, ev: Any) -> None:
        self._completed = ev
        self._blink = False
        if self._blink_timer is not None:
            self._blink_timer.stop()
            self._blink_timer = None
        self._rebuild()

    def _rebuild(self) -> None:
        self.update(Group(*build_tool_parts(self._started, self._completed, blink=self._blink)))


class ToolGroupWidget(SelectableStatic):
    """A collapsed run of consecutive search/read tool calls (claude-code style).

    claude-code coalesces a run of consecutive ``Read``/``Grep``/``Glob`` calls
    into ONE collapsible line ("Searched for 2 patterns, read 1 file"); the run is
    broken by any other transcript event (assistant text, a non-collapsible tool,
    etc.), handled in the app. Collapsed, this row shows the shared
    ``tool_group_summary_text`` one-liner; expanded (``ctrl+o``), it renders each
    tool via the shared :func:`build_tool_parts` so the detail matches a standalone
    ``ToolCallWidget``. Entries correlate their completion by ``tool_use_id``.
    """

    def __init__(self, *, expanded: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Each entry is a mutable ``[started_event, completed_event | None]`` pair.
        self._entries: list[list] = []
        self.expanded = expanded
        self._rebuild()

    def add_started(self, ev: Any) -> None:
        self._entries.append([ev, None])
        self._rebuild()

    def complete(self, ev: Any) -> None:
        tid = getattr(ev, "tool_use_id", None)
        for entry in self._entries:
            if getattr(entry[0], "tool_use_id", None) == tid:
                entry[1] = ev
                break
        self._rebuild()

    def set_expanded(self, flag: bool) -> None:
        self.expanded = flag
        self._rebuild()

    def _rebuild(self) -> None:
        if self.expanded:
            parts: list[Any] = []
            for started, completed in self._entries:
                parts.extend(build_tool_parts(started, completed))
            parts.append(Text(" (ctrl+o 折叠)", style=Palette.DIM))
            self.update(Group(*parts))
            return
        items = [(getattr(started, "tool_name", ""), getattr(started, "headline", "")) for started, _ in self._entries]
        active = any(completed is None for _, completed in self._entries)
        self.update(tool_group_summary_text(items, active=active, expanded=False))


class UserMessageRow(SelectableStatic):
    """The user's own typed message, rendered with the ❯ prompt chevron.

    Mounted (instead of clearing silently) so the human's turn stays in the
    scrollback transcript — the ``PromptInput`` clears on submit, so without this
    the user's message would vanish. Uses the SAME shared ``user_message_row``
    builder as the rich terminal so both hosts look identical.
    """

    def __init__(self, text: str, **kwargs: Any) -> None:
        super().__init__(user_message_row(text), **kwargs)


class MediaRow(SelectableStatic):
    """A media artifact — an image is painted inline, else a reference line.

    An ``image`` media kind whose ``ref`` is a readable file is rendered inline as
    truecolor half-block pixels (``render_image``); any non-image, an unreadable
    ref, or a Pillow/decoder failure degrades to the labelled reference line the
    terminal host also uses, so a text-only terminal never sees garbage.
    """

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        label = getattr(ev, "media_kind", None) or "media"
        ref = getattr(ev, "ref", None) or ""
        caption = media_caption(ev)
        image = render_image(ref) if label == "image" and ref and os.path.isfile(ref) else None
        if image is not None:
            super().__init__(Group(caption, indent(image, RESULT_INDENT)), **kwargs)
        else:
            super().__init__(caption, **kwargs)


class FileDiffRow(SelectableStatic):
    """A structured file change (Edit / apply_patch) — a caption + coloured diff.

    The change rides as ``old``/``new`` full contents on the ``FileDiffBlock``;
    this text host synthesizes a coloured unified diff from them via the shared
    ``render_file_change`` builder (identical look to the rich terminal). A future
    media-capable host could instead mount an interactive side-by-side from the
    same facts — the block carries the fact, not a pre-formatted diff string.
    """

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        old = getattr(ev, "old", "") or ""
        new = getattr(ev, "new", "") or ""
        path = getattr(ev, "path", "") or ""
        caption = file_change_caption(ev)
        diff = indent(render_file_change(old, new, path), RESULT_INDENT)
        super().__init__(Group(caption, diff), **kwargs)


class NoticeRow(SelectableStatic):
    """A system notice (info / warning / success)."""

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        style = notice_style(getattr(ev, "level", "info"))
        super().__init__(linkify(getattr(ev, "text", "") or "", base_style=style), **kwargs)


class SystemReminderRow(SelectableStatic):
    """A framework-injected ``<system-reminder>``, condensed to a dim ⚑ note.

    mote injects per-turn context (git/token/changed-files/skill/tool/
    compaction) as a ``<system-reminder>`` block the model sees but the human
    otherwise wouldn't. The projector already summarized it to a heading line;
    this renders it dim + ⚑ so the human sees *what* was fed to the model without
    the raw prose crowding the transcript.
    """

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        text = Text()
        text.append(NOTE + " ", style=Palette.DIM)
        text.append_text(linkify(getattr(ev, "text", "") or "", base_style=Palette.DIM))
        super().__init__(text, **kwargs)


class ConversationCompactedRow(SelectableStatic):
    """A dim ``✻ 对话已压缩`` boundary marker (claude-code's compacted line).

    History was compacted (context filled up); the engine condensed earlier turns
    into a summary now at the top of the model's context. This row marks *where*
    that happened in the transcript so the human isn't puzzled by the jump — using
    the SAME shared ``conversation_compacted_text`` builder as the rich terminal.
    """

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        super().__init__(conversation_compacted_text(ev), **kwargs)


class CompactionSummaryRow(SelectableStatic):
    """The dim, folded compaction *recap* re-rendered after a full-screen clear.

    Textual is an alt-buffer app with no native scrollback, so on compaction the
    app wipes the stale transcript (see ``MoteApp._on_conversation_compacted``)
    and re-mounts this recap as the on-screen bridge to what came before — folded
    via the shared ``compaction_summary_text`` builder so a long recap stays tidy.
    """

    def __init__(self, summary: str, **kwargs: Any) -> None:
        super().__init__(compaction_summary_text(summary), **kwargs)


class ErrorRow(SelectableStatic):
    """A turn-level error surfaced to the user."""

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        super().__init__(
            bullet_row(
                BULLET,
                linkify(getattr(ev, "text", "") or "", base_style=Palette.ERROR),
                style=f"bold {Palette.ERROR}",
            ),
            **kwargs,
        )


class TaskProgressRow(SelectableStatic):
    """A background-task progress line (running / success / failed / other)."""

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        super().__init__(task_progress_text(ev), **kwargs)


class QuestionMarkerRow(SelectableStatic):
    """A neutral marker that the agent posed a question (the modal collects it)."""

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        line = Text()
        line.append(BULLET + " ", style=Palette.QUESTION)
        line.append("? ", style=f"bold {Palette.QUESTION}")
        line.append(getattr(ev, "question", ""), style=Palette.QUESTION)
        super().__init__(line, **kwargs)


class ApprovalMarkerRow(SelectableStatic):
    """A neutral marker that a gated action awaits approval (the modal decides)."""

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        action = getattr(ev, "action", None) or getattr(ev, "tool_name", None) or "action"
        line = Text()
        line.append(BULLET + " ", style=Palette.WARNING)
        line.append(f"{WARN} approval required ", style=f"bold {Palette.WARNING}")
        line.append(f"[{getattr(ev, 'risk', 'medium')}] ", style=Palette.DIM)
        line.append(action, style=Palette.WARNING)
        super().__init__(line, **kwargs)


class SessionListWidget(SelectableStatic):
    """The resumable-session list rendered as a numbered rich table."""

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        from mote.cli.consumers.render.builders import session_table

        items = getattr(ev, "items", None)
        if not items:
            super().__init__(Text("  (no sessions)", style=Palette.DIM), **kwargs)
            return
        table = session_table(ev)
        super().__init__(table if table is not None else Text("  (no sessions)", style=Palette.DIM), **kwargs)


__all__ = [
    "AssistantBlock",
    "build_tool_parts",
    "ToolCallWidget",
    "ToolGroupWidget",
    "UserMessageRow",
    "MediaRow",
    "FileDiffRow",
    "NoticeRow",
    "SystemReminderRow",
    "ConversationCompactedRow",
    "CompactionSummaryRow",
    "ErrorRow",
    "TaskProgressRow",
    "QuestionMarkerRow",
    "ApprovalMarkerRow",
    "SessionListWidget",
]
