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
from textual.message import Message
from textual.reactive import reactive

from mote.cli.consumers.render.builders import (
    RESULT_INDENT,
    FoldMode,
    bullet_row,
    compaction_summary_text,
    conversation_compacted_text,
    file_change_caption,
    fold_mode,
    indent,
    is_rejection,
    linkify,
    media_caption,
    notice_style,
    render_file_change,
    render_image,
    render_result_detail,
    session_table,
    task_progress_text,
    tool_body_syntax,
    tool_completed_text,
    tool_group_summary_text,
    tool_started_text,
)
from mote.cli.consumers.render.markdown import themed_markdown
from mote.cli.consumers.textual.style import BULLET, NOTE, WARN, Palette
from mote.cli.consumers.textual.widgets.base import SelectableStatic
from mote.common.i18n import keys as K
from mote.common.i18n import t


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
    parts: list[Any] = [
        tool_started_text(started, ok=ok, blink=blink and completed is None, rejected=is_rejection(completed))
    ]
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


class FoldableRow(SelectableStatic):
    """A transcript row whose detail folds under the ``ctrl+o`` toggle.

    The single home for the fold state + toggle entry point, so the host's
    ``ctrl+o`` handler flips every mounted foldable row in ONE pass
    (``self.query(FoldableRow)``) and never enumerates concrete widget types.
    Subclasses (``ToolGroupWidget`` = coalesced search/read summary,
    ``ToolCallWidget`` = per-call detail) own *what* the folded vs expanded view
    looks like by implementing :meth:`_rebuild` to read :attr:`expanded`; they
    call it once their own state is initialised.

    A foldable row is also **clickable**: hovering it tints the background
    (``:hover``) so the human sees it's interactive, and a plain click posts a
    :class:`Clicked` message the host uses to *select* the row (distinct
    ``-selected`` background). While a row is selected, ``ctrl+o`` scopes to just
    that row instead of toggling every row (see ``MoteApp``), and a dim
    ``ctrl+o 展开/折叠`` hint rides the row's bottom-right corner (the affordance
    lives on the selected block now, not on the status bar). A coalesced
    search/read run is ONE ``ToolGroupWidget``, so selecting it picks the whole
    group together and shows a single hint under it.
    """

    #: Hover tint (interactive affordance) + a distinct band for the selected row.
    #: These sit *behind* the row's own content segments (which are mostly
    #: bg-transparent) so the shared builder colours still show through.
    DEFAULT_CSS = """
    FoldableRow:hover {
        background: $boost;
    }
    FoldableRow.-selected {
        background: $dim 40%;
    }
    """

    #: Whether this row is the host's currently-selected one (drives ``-selected``).
    selected: reactive[bool] = reactive(False)

    class Clicked(Message):
        """A foldable row was plain-clicked — the host selects it for scoped ctrl+o.

        Bubbles to the app, whose ``on_foldable_row_clicked`` toggles the single
        selection. Carries the concrete row so the handler needn't re-query.
        """

        def __init__(self, row: "FoldableRow") -> None:
            super().__init__()
            self.row = row

    def __init__(self, *, expanded: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.expanded = expanded
        # The subclass' bare renderable (before the selected-row hint is layered
        # on). Kept so toggling ``selected`` can re-decorate without a rebuild.
        self._core: Any = ""

    def watch_selected(self, value: bool) -> None:
        """Toggle the ``-selected`` band + (re)layer the bottom-right ctrl+o hint."""
        self.set_class(value, "-selected")
        self._repaint()

    @property
    def _fold_hint(self) -> bool:
        """Whether a selected-row should advertise ``ctrl+o`` (i.e. it truly folds)."""
        return True

    def update(self, renderable: Any = "") -> None:
        """Store the subclass renderable, then paint it (plus the hint if selected).

        Subclasses call ``self.update(...)`` from :meth:`_rebuild`; we intercept
        so the currently-selected row gets its ``ctrl+o`` hint appended without
        each subclass having to know about selection.
        """
        self._core = renderable
        self._repaint()

    def _repaint(self) -> None:
        """Paint ``_core``, appending the dim bottom-right ctrl+o hint when selected.

        NB: deliberately *not* named ``_render_content`` — that is a Textual
        ``Widget`` internal that fills ``_render_cache`` (the strips ``get_selection``
        reads); shadowing it would silently break copy/selection on every row.
        """
        core = self._core
        if self.selected and self._fold_hint:
            label = t(K.KEY_COLLAPSE_HINT) if self.expanded else t(K.KEY_EXPAND_HINT)
            hint = Text(label, style=Palette.DIM, justify="right")
            core = Group(core, hint)
        super().update(core)

    def set_expanded(self, flag: bool) -> None:
        """Fold/unfold this row — re-renders only on an actual state change."""
        if flag != self.expanded:
            self.expanded = flag
            self._rebuild()

    async def _on_click(self, event: Any) -> None:
        """Post :class:`Clicked` on a plain click so the host can select this row.

        A Ctrl+click is a link nav, so it defers to ``SelectableStatic._on_click``
        (the URL-open handler) via ``super()``; a click that ended a drag-select
        (``text_selection`` set) is a copy gesture we leave to the base handler.
        Only a *plain* click adds row-selection — and there we do NOT call super,
        since Textual's own ``Widget._on_click`` (double/triple-click text-select)
        walks the MRO on its own.
        """
        if getattr(event, "ctrl", False) or self.text_selection is not None:
            await super()._on_click(event)
            return
        self.post_message(self.Clicked(self))

    def _rebuild(self) -> None:  # pragma: no cover - overridden by every subclass
        raise NotImplementedError


class AssistantBlock(FoldableRow):
    """A streaming assistant (or reasoning) markdown block.

    Accumulates deltas in ``_buf`` and re-renders the whole markdown on each
    ``append_delta`` — Textual handles the incremental repaint, so there is no
    need for the terminal consumer's block-boundary ``Live`` region.

    As a :class:`FoldableRow` the block is click-selectable and folds under
    ``ctrl+o`` just like a tool row: collapsed it keeps only the first line plus a
    dim ``… +N 行`` tail, expanded it renders the full markdown. It starts
    **expanded** — a fresh reply/reasoning block must stay readable while it
    streams — and only collapses on an explicit toggle (global ``ctrl+o`` or a
    click-select scoped ``ctrl+o``).
    """

    def __init__(self, **kwargs: Any) -> None:
        # Default expanded: the block must stay fully visible while it streams;
        # ctrl+o (global or scoped to a click-selected block) folds it afterwards.
        super().__init__(expanded=True, **kwargs)
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

    @property
    def _fold_hint(self) -> bool:
        # Only advertise the ctrl+o affordance when there's more than one line to
        # hide — folding a single-line reply would be a visual no-op.
        return len(self._buf.strip().splitlines()) > 1

    def _rebuild(self) -> None:
        if not self._buf.strip():
            return
        if self.expanded:
            self.update(bullet_row(BULLET, themed_markdown(self._buf), style=Palette.BRAND))
            return
        # Folded: the first line rendered as markdown + a dim "… +N 行" tail so a
        # long block collapses to one glanceable line (same fold wording as the
        # compaction recap).
        lines = self._buf.strip().splitlines()
        head = themed_markdown(lines[0])
        hidden = len(lines) - 1
        renderable = head if hidden <= 0 else Group(head, Text(t(K.FOLD_MORE_LINES, count=hidden), style=Palette.DIM))
        self.update(bullet_row(BULLET, renderable, style=Palette.BRAND))


class ToolCallWidget(FoldableRow):
    """One tool invocation + its result, keyed by ``tool_use_id``.

    Built from the ``ToolCallStarted`` event; :meth:`complete` folds in the
    matching ``ToolCallCompleted`` (correlated by ``tool_use_id`` in the app) so
    the started line, optional body, result summary and structured detail
    (diff / table) render together as one transcript row.

    Every tool call is foldable under ``ctrl+o``: collapsed (following the global
    toggle) it keeps only the ``● Tool(headline)`` line and the ``⎿ summary``
    result, hiding the command body + full output / structured detail; expanded it
    renders the full :func:`build_tool_parts`. (Search/read runs coalesce into a
    :class:`ToolGroupWidget` instead, so they never reach here.)
    """

    #: The running bullet pulses at this cadence (secs) until the call completes.
    _BLINK_INTERVAL = 0.5

    def __init__(self, ev: Any, *, expanded: bool = False, **kwargs: Any) -> None:
        super().__init__(expanded=expanded, **kwargs)
        self.tool_use_id: Optional[str] = getattr(ev, "tool_use_id", None)
        self._started = ev
        self._completed: Any = None
        self._blink = False
        self._blink_timer: Any = None
        self._folds_detail = fold_mode(getattr(ev, "tool_name", "")) is FoldMode.DETAIL
        # Structured file changes (Edit/Write) folded INTO this row so the
        # invocation + its diff are ONE select/fold unit; each entry is the
        # ``[caption, diff]`` parts of one changed file (a tool may touch several).
        self._file_diffs: list[list[Any]] = []
        self._rebuild()

    def set_file_diff(self, ev: Any) -> None:
        """Fold a structured ``FileDiffBlock`` into this row (Edit/Write's diff).

        The diff becomes part of the tool row instead of a standalone
        :class:`FileDiffRow`, so clicking selects — and ``ctrl+o`` folds — the
        invocation line and its change together. A file change's diff is the point
        of an Edit/Write, so attaching one **expands** the row (a NONE-fold tool
        that otherwise always rendered full); ``ctrl+o`` then folds it to the
        ``● Tool`` + ``⎿ summary`` lines.
        """
        self._file_diffs.append(file_diff_parts(ev))
        if not self._folds_detail:
            self.expanded = True
        self._rebuild()

    @property
    def _folds(self) -> bool:
        # Detail-folding calls (Bash/Terminal/WebBrowser) toggle under ctrl+o; a
        # standalone Read always renders full, so it shows no fold hint — EXCEPT
        # when the call was DENIED, or when it carries a folded-in file diff
        # (Edit/Write). A rejection never ran, so its body (the would-be
        # diff/content) is dead weight; folding it to just the ``● Tool`` +
        # ``⎿ rejected`` lines reclaims the space. An Edit/Write's diff folds
        # away too so the invocation + change collapse as one row.
        return self._folds_detail or is_rejection(self._completed) or bool(self._file_diffs)

    @property
    def _fold_hint(self) -> bool:
        return self._folds

    def on_mount(self) -> None:
        # Pulse the running bullet until the result lands (a blink);
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
        if self._folds and not self.expanded:
            # Folded: keep only the invocation line and (once done) the result
            # summary, hiding the body + full output (and any folded-in diff). The
            # ctrl+o affordance shows only on the selected block, not every row.
            ok = None if self._completed is None else bool(getattr(self._completed, "ok", True))
            head = tool_started_text(
                self._started,
                ok=ok,
                blink=self._blink and self._completed is None,
                rejected=is_rejection(self._completed),
            )
            if self._completed is None:
                self.update(head)
                return
            self.update(Group(head, tool_completed_text(self._completed)))
            return
        parts = build_tool_parts(self._started, self._completed, blink=self._blink)
        # Append any structured file diffs (Edit/Write) so the change renders
        # inside this row, below the invocation + result — one select/fold unit.
        for diff_parts in self._file_diffs:
            parts.extend(diff_parts)
        self.update(Group(*parts))


class ToolGroupWidget(FoldableRow):
    """A collapsed run of consecutive search/read tool calls.

    A run of consecutive ``Read``/``Grep``/``Glob`` calls coalesces
    into ONE collapsible line ("Searched for 2 patterns, read 1 file"); the run is
    broken by any other transcript event (assistant text, a non-folding tool,
    etc.), handled in the app. Collapsed, this row shows the shared
    ``tool_group_summary_text`` one-liner; expanded (``ctrl+o``), it renders each
    tool via the shared :func:`build_tool_parts` so the detail matches a standalone
    ``ToolCallWidget``. Entries correlate their completion by ``tool_use_id``.
    """

    def __init__(self, *, expanded: bool = False, **kwargs: Any) -> None:
        super().__init__(expanded=expanded, **kwargs)
        # Each entry is a mutable ``[started_event, completed_event | None]`` pair.
        self._entries: list[list] = []
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

    def _rebuild(self) -> None:
        if self.expanded:
            parts: list[Any] = []
            for started, completed in self._entries:
                parts.extend(build_tool_parts(started, completed))
            self.update(Group(*parts))
            return
        items = [(getattr(started, "tool_name", ""), getattr(started, "headline", "")) for started, _ in self._entries]
        active = any(completed is None for _, completed in self._entries)
        self.update(tool_group_summary_text(items, active=active))


class UserMessageRow(SelectableStatic):
    """The user's own typed message — a full-width blue band, WeChat-style right side.

    Mounted (instead of clearing silently) so the human's turn stays in the
    scrollback transcript — the ``PromptInput`` clears on submit, so without this
    the user's message would vanish.

    The row spans the **whole width** as a blue band (drawn at reduced opacity so
    it reads as a soft tinted slab, not a hard block) but its content is
    **right-justified** so the human's turn reads as the right-hand side of a
    chat, mirroring the assistant's left-aligned ``●`` blocks. A blue ``●``
    speaker dot rides the far right edge after the text — the mirror of the
    assistant's brand-orange ``●`` gutter marker on the left.

    As a :class:`SelectableStatic` the text stays mouse-selectable / copyable
    like every other transcript row, and its blue background sits *behind* the
    (bg-transparent) text so the right-justified content shows through.
    """

    #: The speaker dot at the band's right edge — a clear blue (the mirror of the
    #: assistant's brand ``●``), bright enough to read on the translucent band.
    _BULLET_BLUE = "#4a9eff"

    #: Checkbox glyphs shown at the left gutter while ``select_mode`` is on.
    _BOX_EMPTY = "☐"
    _BOX_CHECKED = "☑"

    DEFAULT_CSS = """
    UserMessageRow {
        background: #12507e 55%;
        content-align-horizontal: right;
    }
    UserMessageRow.-checked {
        background: #1e6db0 70%;
    }
    """

    #: Whether the host is in react-unit delete-mode (shows the checkbox gutter).
    select_mode: reactive[bool] = reactive(False)
    #: Whether this row's checkbox is ticked (part of the pending delete set).
    selected: reactive[bool] = reactive(False)

    def __init__(self, text: str, *, message_id: Optional[str] = None, **kwargs: Any) -> None:
        self._text = text
        #: The stored ``Message.id`` this row was rendered from — the react-unit
        #: delete anchor. ``None`` for rows rendered before ids were threaded
        #: (e.g. a resumed transcript); such a row can't be a delete anchor.
        self.message_id = message_id
        # Reactives can't be read before ``super().__init__`` runs (the node has
        # no data yet), and both default to False anyway → build the initial
        # (no-checkbox) renderable inline; ``watch_select_mode`` rebuilds with the
        # gutter once delete-mode arms.
        super().__init__(self._build_line(select_mode=False, selected=False), **kwargs)

    def _build_line(self, *, select_mode: bool, selected: bool) -> Text:
        """Build the row renderable, prefixing a checkbox when in select-mode.

        Named ``_build_line`` (not ``_render``) to avoid shadowing Textual's own
        ``Widget._render`` used during layout.
        """
        # Right-justify the whole line so a short turn hugs the right edge and a
        # long one right-aligns as it wraps; the blue ``●`` trails the text.
        line = Text(justify="right")
        if select_mode:
            glyph = self._BOX_CHECKED if selected else self._BOX_EMPTY
            line.append(glyph + " ", style=self._BULLET_BLUE)
        line.append(self._text)
        line.append(" " + BULLET, style=self._BULLET_BLUE)
        return line

    def _rebuild(self) -> None:
        self.update(self._build_line(select_mode=self.select_mode, selected=self.selected))

    def watch_select_mode(self, value: bool) -> None:
        # Leaving delete-mode clears any tick so a re-entry starts clean.
        if not value:
            self.selected = False
        self._rebuild()

    def watch_selected(self, value: bool) -> None:
        self.set_class(value, "-checked")
        self._rebuild()

    def toggle(self) -> None:
        """Flip the checkbox (only meaningful while ``select_mode``)."""
        if self.select_mode:
            self.selected = not self.selected

    async def _on_click(self, event: Any) -> None:
        # In delete-mode a plain click toggles the checkbox (and does NOT start a
        # text selection); otherwise fall through to the base row behaviour
        # (drag-select / Ctrl+click link following). The app re-queries every
        # ticked row on confirm, so no per-toggle message is bubbled.
        if self.select_mode and not getattr(event, "ctrl", False):
            event.stop()
            self.toggle()
            return
        await super()._on_click(event)


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


def file_diff_parts(ev: Any) -> list:
    """Renderables for one structured file change: ``⎿ path (verb)`` caption + diff.

    The change rides as ``old``/``new`` full contents on the ``FileDiffBlock``;
    this text host synthesizes a coloured unified diff from them via the shared
    ``render_file_change`` builder (identical look to the rich terminal). Shared by
    :class:`ToolCallWidget` (which folds an Edit/Write's diff *into* the tool row
    so the invocation + change select/fold as ONE unit) and the standalone
    :class:`FileDiffRow` fallback (a diff with no owning tool row).
    """
    old = getattr(ev, "old", "") or ""
    new = getattr(ev, "new", "") or ""
    path = getattr(ev, "path", "") or ""
    return [file_change_caption(ev), indent(render_file_change(old, new, path), RESULT_INDENT)]


class FileDiffRow(FoldableRow):
    """A structured file change with **no owning tool row** — caption + coloured diff.

    The normal Edit/Write path folds the diff *into* its :class:`ToolCallWidget`
    (so the invocation + change are one select/fold unit); this standalone row is
    the fallback for a ``FileDiffBlock`` whose ``tool_use_id`` matched no mounted
    tool widget. A future media-capable host could mount an interactive
    side-by-side from the same ``old``/``new`` facts instead of the synthesized
    diff — the block carries the fact, not a pre-formatted diff string.

    As a :class:`FoldableRow` it is click-selectable and folds under ``ctrl+o``:
    collapsed it keeps only the ``⎿ path (verb)`` caption; expanded it renders the
    caption + full diff. It starts **expanded** — the "what changed" is the point.
    """

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        # Default expanded: the diff is the point of a file change; ctrl+o (global
        # or scoped to a click-selected row) folds it to just the caption after.
        super().__init__(expanded=True, **kwargs)
        self._parts = file_diff_parts(ev)
        self._rebuild()

    def _rebuild(self) -> None:
        if self.expanded:
            self.update(Group(*self._parts))
            return
        # Folded: keep only the caption naming the file + verb; the diff lives
        # behind ctrl+o now (the affordance shows on the selected row's corner).
        self.update(self._parts[0])


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
    """A dim ``✻ 对话已压缩`` boundary marker for the compacted line.

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
        line.append(f"{WARN} {t(K.APPROVAL_REQUIRED)} ", style=f"bold {Palette.WARNING}")
        line.append(f"[{getattr(ev, 'risk', 'medium')}] ", style=Palette.DIM)
        line.append(action, style=Palette.WARNING)
        super().__init__(line, **kwargs)


class SessionListWidget(SelectableStatic):
    """The resumable-session list rendered as a numbered rich table."""

    def __init__(self, ev: Any, **kwargs: Any) -> None:
        items = getattr(ev, "items", None)
        if not items:
            super().__init__(Text("  (no sessions)", style=Palette.DIM), **kwargs)
            return
        table = session_table(ev)
        super().__init__(table if table is not None else Text("  (no sessions)", style=Palette.DIM), **kwargs)


__all__ = [
    "AssistantBlock",
    "build_tool_parts",
    "FoldableRow",
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
