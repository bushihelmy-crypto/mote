#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TerminalSurface`` — land the neutral op stream on a scrolling rich console.

The scrolling host's :class:`RenderSurface`: each op method realises one decision
the :class:`TranscriptReducer` already made, using the rich primitives that used
to live inline in the old ``TerminalConsumer`` — the incremental-Markdown live
region (``_stream`` / ``_end_stream`` / ``_commit`` / ``_show_tail``) that commits
finalized blocks and keeps only a small erasable tail, the transient retry
countdown, the ``● Tool(args)`` / ``⎿ summary`` bullet+branch layout, the
three-tier image renderer.

Two behaviours **converge** onto the terminal here that only the Textual host had
before, because the reducer now drives them for both:

* **Collapsed search/read groups** — a run of Read/Grep/Glob is buffered and
  printed as a single ``● 搜索 N · 读取 M`` summary line on ``flush_group`` (a
  linear scrollback can't retro-fold already-printed rows, so it buffers until
  the run breaks and prints once).
* **A ``✻ 思考中`` thinking indicator** — reasoning tokens no longer stream into
  the permanent scrollback; instead ``set_thinking`` opens a transient ``Live``
  (erased the moment the visible reply / a tool / any other event arrives), the
  scrolling analogue of the Textual ``StatusBar.thinking`` reactive.

``rich``'s single-``Live`` constraint is honoured for free: the reducer emits
``set_thinking(False)`` / ``clear_retry`` before any event that would open the
markdown ``Live``, so the thinking, retry and streaming regions never coexist.

Interaction (ctrl+o fold, click-to-select) is impossible on a linear terminal, so
DETAIL tools render **expanded** and there is no fold state to toggle — a
capability difference, not a missing feature.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

from mote.cli.consumers.render.builders import CONTENT_INDENT as _CONTENT_INDENT
from mote.cli.consumers.render.builders import RESULT_INDENT as _RESULT_INDENT
from mote.cli.consumers.render.builders import FoldMode, bullet_row, conversation_compacted_text, file_change_caption
from mote.cli.consumers.render.builders import fold_note as _fold_note
from mote.cli.consumers.render.builders import format_usage_line as _format_usage_line
from mote.cli.consumers.render.builders import indent as _indent_renderable
from mote.cli.consumers.render.builders import linkify, media_caption, notice_style, render_file_change
from mote.cli.consumers.render.builders import render_image as _render_image
from mote.cli.consumers.render.builders import (
    render_result_detail,
    task_progress_text,
    tool_body_syntax,
    tool_completed_text,
    tool_group_summary_text,
    user_message_row,
)
from mote.cli.consumers.render.markdown import themed_markdown
from mote.cli.consumers.render.palette import BULLET, COMPACT, NOTE, RETRY, WARN, Palette
from mote.cli.consumers.render.terminal_image import detect_image_protocol
from mote.cli.consumers.transcript import BaseSurface, Truncation
from mote.common.i18n import keys as K
from mote.common.i18n import t

try:  # rich is optional — this surface is only built when it is present.
    from rich import box
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text

    _HAS_RICH = True
except ImportError:  # pragma: no cover — the plain fallback consumer is used instead
    _HAS_RICH = False


class TerminalSurface(BaseSurface):
    """Land each :class:`TranscriptOp` on a scrolling rich console."""

    #: Repaint cadence (Hz) for the transient ``Live`` regions (streaming markdown
    #: tail + retry countdown + thinking indicator).
    _LIVE_REFRESH_PER_SECOND = 12

    def __init__(self, console: Optional["Console"] = None):
        self._console = console if console is not None else Console()
        # A native inline-image protocol (Kitty/…) if this terminal speaks one —
        # detected once (a cheap env sniff). ``None`` => half-block fallback.
        self._image_protocol = detect_image_protocol()
        # Live-markdown streaming state (the trailing, not-yet-finalized block).
        self._live: Optional["Live"] = None
        self._pending = ""
        # The transient retry countdown / thinking indicator — each an erasable
        # ``Live`` wiped the moment the reducer sequences its clearing op.
        self._retry_live: Optional["Live"] = None
        self._thinking_live: Optional["Live"] = None
        # Whether the current assistant turn has printed its ``●`` bullet.
        self._assistant_open = False
        # A buffered search/read run: ``(tool_name, headline)`` per grouped call,
        # plus the count still in flight. ``None`` => no open group.
        self._group_items: Optional[List[Tuple[str, str]]] = None
        self._group_pending = 0

    # ------------------------------------------------------------------
    # Small layout helpers (bullet column + indented continuation)
    # ------------------------------------------------------------------
    def _bullet_row(self, glyph: str, renderable: Any, *, style: str):
        return bullet_row(glyph, renderable, style=style)

    @staticmethod
    def _indent(renderable: Any, spaces: int = _CONTENT_INDENT):
        return _indent_renderable(renderable, spaces)

    # ------------------------------------------------------------------
    # block lifecycle
    # ------------------------------------------------------------------
    def open_block(self, role: str) -> None:
        # A streaming region opens; nothing to draw until deltas arrive.
        return None

    def append_delta(self, text: str, reasoning: bool) -> None:
        if reasoning:
            # Reasoning is surfaced only as the transient ``✻ 思考中`` indicator
            # (opened by ``set_thinking``); its tokens never enter the scrollback.
            return
        self._stream(text)

    def close_block(self, markdown: str, streamed: bool, truncation: Truncation) -> None:
        if streamed:
            # The live region already rendered it incrementally — just finalize.
            self._end_stream()
            self._show_truncation(truncation, spaces=_CONTENT_INDENT)
            return
        # Non-streamed (or downgraded) block: render the markdown fresh, bulleted.
        self._end_stream()
        if markdown.strip():
            self._console.print()
            self._console.print(self._bullet_row(BULLET, themed_markdown(markdown), style=Palette.BRAND))
        self._show_truncation(truncation, spaces=_CONTENT_INDENT)

    def render_user_message(self, markdown: str) -> None:
        self._end_stream()
        if markdown.strip():
            self._console.print()
            self._console.print(user_message_row(markdown))

    # ------------------------------------------------------------------
    # standalone tools (NONE / DETAIL — the terminal renders both expanded)
    # ------------------------------------------------------------------
    def tool_started(self, ev: Any, fold: FoldMode) -> None:
        self._end_stream()
        self._console.print()
        line = Text()
        line.append(BULLET + " ", style=Palette.BRAND)
        line.append(ev.title or ev.tool_name, style=f"bold {Palette.BRAND}")
        if ev.headline:
            line.append("(", style=Palette.DIM)
            line.append(ev.headline, style=Palette.DIM)
            line.append(")", style=Palette.DIM)
        self._console.print(line)
        body = tool_body_syntax(ev)
        if body is not None:
            self._console.print(self._indent(body, _RESULT_INDENT))

    def tool_completed(self, ev: Any, fold: FoldMode, truncation: Truncation) -> None:
        self._end_stream()
        self._console.print(tool_completed_text(ev))
        for part in render_result_detail(ev, _RESULT_INDENT):
            self._console.print(part)
        self._show_truncation(truncation, spaces=_RESULT_INDENT)

    # ------------------------------------------------------------------
    # collapsed search/read group (buffered → one summary line on flush)
    # ------------------------------------------------------------------
    def open_group(self) -> None:
        self._end_stream()
        self._group_items = []
        self._group_pending = 0

    def add_to_group(self, ev: Any) -> None:
        if self._group_items is None:  # defensive — open_group always precedes
            self._group_items = []
        self._group_items.append((getattr(ev, "tool_name", "") or "", getattr(ev, "headline", "") or ""))
        self._group_pending += 1

    def complete_in_group(self, ev: Any) -> None:
        # A completion that lands after the run was flushed (the linear ceiling:
        # its line is already printed and can't be repainted) is a no-op.
        if self._group_items is not None and self._group_pending > 0:
            self._group_pending -= 1

    def flush_group(self) -> None:
        items, self._group_items = self._group_items, None
        pending, self._group_pending = self._group_pending, 0
        if not items:
            return
        summary = tool_group_summary_text(items, active=pending > 0)
        if summary.plain:
            self._console.print()
            self._console.print(summary)

    # ------------------------------------------------------------------
    # static transcript rows
    # ------------------------------------------------------------------
    def render_media(self, ev: Any) -> None:
        self._end_stream()
        label = ev.media_kind or "media"
        self._console.print(media_caption(ev))
        path = ev.ref or ""
        is_image = label == "image" and bool(path) and os.path.isfile(path)
        if is_image and self._render_native_image(path):
            return
        if is_image:
            image = _render_image(path)
            if image is not None:
                self._console.print(self._indent(image, _RESULT_INDENT))

    def render_file_diff(self, ev: Any) -> None:
        self._end_stream()
        old = getattr(ev, "old", "") or ""
        new = getattr(ev, "new", "") or ""
        path = getattr(ev, "path", "") or ""
        self._console.print(file_change_caption(ev))
        self._console.print(self._indent(render_file_change(old, new, path), _RESULT_INDENT))

    def render_task_progress(self, ev: Any) -> None:
        self._end_stream()
        self._console.print(task_progress_text(ev))

    def render_notice(self, ev: Any) -> None:
        self._end_stream()
        self._console.print(linkify(ev.text, base_style=notice_style(ev.level)))

    def render_system_reminder(self, ev: Any) -> None:
        self._end_stream()
        line = Text()
        line.append(NOTE + " ", style=Palette.DIM)
        line.append_text(linkify(getattr(ev, "text", "") or "", base_style=Palette.DIM))
        self._console.print(line)

    def render_error(self, ev: Any) -> None:
        self._end_stream()
        self._console.print()
        self._console.print(
            self._bullet_row(BULLET, linkify(ev.text, base_style=Palette.ERROR), style=f"bold {Palette.ERROR}")
        )

    def render_question(self, ev: Any) -> None:
        self._end_stream()
        line = Text()
        line.append(BULLET + " ", style=Palette.QUESTION)
        line.append("? ", style=f"bold {Palette.QUESTION}")
        line.append(ev.question, style=Palette.QUESTION)
        self._console.print(line)

    def render_approval(self, ev: Any) -> None:
        self._end_stream()
        action = ev.action or ev.tool_name or "action"
        line = Text()
        line.append(BULLET + " ", style=Palette.WARNING)
        line.append(f"{WARN} {t(K.APPROVAL_REQUIRED)} ", style=f"bold {Palette.WARNING}")
        line.append(f"[{ev.risk}] ", style=Palette.DIM)
        line.append(action, style=Palette.WARNING)
        self._console.print(line)

    def render_session_list(self, ev: Any) -> None:
        self._end_stream()
        if not ev.items:
            self._console.print(Text("  (no sessions)", style=Palette.DIM))
            return
        table = Table(title=ev.title, show_header=True, header_style=f"bold {Palette.BRAND}", box=box.SIMPLE)
        table.add_column("#", style=Palette.BRAND, justify="right")
        table.add_column("Session")
        table.add_column("Updated", style=Palette.DIM)
        table.add_column("Preview", style=Palette.DIM)
        for item in ev.items:
            table.add_row(
                str(item.index),
                item.label or item.session_id,
                item.updated_at or "",
                item.preview or "",
            )
        self._console.print(table)

    # ------------------------------------------------------------------
    # transient chrome (never a permanent transcript row)
    # ------------------------------------------------------------------
    def set_thinking(self, on: bool) -> None:
        if on:
            # Close any open markdown/retry region first (rich allows one Live),
            # then open the erasable ``✻ 思考中…`` indicator.
            self._end_stream()
            text = Text()
            text.append(f"{COMPACT} ", style=f"bold {Palette.BRAND}")
            text.append(t(K.STATUS_THINKING) + "…", style=Palette.DIM)
            self._thinking_live = Live(
                text,
                console=self._console,
                refresh_per_second=self._LIVE_REFRESH_PER_SECOND,
                vertical_overflow="crop",
                transient=True,
            )
            self._thinking_live.start()
        else:
            self._clear_thinking()

    def set_retry(self, ev: Any) -> None:
        # A *transient* countdown line: render it in its own erasable Live so the
        # next event wipes it — it must never land in the permanent scrollback.
        self._end_stream()  # collapse any open stream / thinking region first
        secs = max(0, round((getattr(ev, "delay_ms", 0.0) or 0.0) / 1000.0))
        etype = getattr(ev, "error_type", "") or "error"
        text = Text()
        text.append(f"{RETRY} ", style=f"bold {Palette.WARNING}")
        text.append(t(K.RETRY_FAILED, error_type=etype), style=Palette.DIM)
        text.append(f" · {t(K.RETRY_ATTEMPT, attempt=ev.attempt, total=ev.max_attempts)} · ", style=Palette.WARNING)
        text.append(t(K.RETRY_COUNTDOWN, secs=secs), style=f"bold {Palette.BRAND}")
        self._retry_live = Live(
            text,
            console=self._console,
            refresh_per_second=self._LIVE_REFRESH_PER_SECOND,
            vertical_overflow="crop",
            transient=True,
        )
        self._retry_live.start()

    def clear_retry(self) -> None:
        self._clear_retry()

    def update_usage(self, ev: Any) -> None:
        self._end_stream()
        line = _format_usage_line(ev)
        if line:
            self._console.print(self._indent(Text("· " + line, style=Palette.DIM)))

    # ------------------------------------------------------------------
    # boundaries / destructive
    # ------------------------------------------------------------------
    def clear_for_compaction(self, summary: str, message_count: int, last_user_prompt: str) -> None:
        # A scrolling terminal keeps the full history above, so a compaction only
        # prints a dim ``✻`` boundary in place — no screen wipe, no recap rebuild.
        self._end_stream()
        self._console.print()
        self._console.print(conversation_compacted_text(SimpleNamespace(message_count=message_count)))

    def clear_transcript(self) -> None:
        # ``/clear`` — end any open stream, then wipe the scrollback for a fresh screen.
        self._end_stream()
        self._console.clear()

    def close(self) -> None:
        self._end_stream()

    # ------------------------------------------------------------------
    # rendering primitives (native image, fold note)
    # ------------------------------------------------------------------
    def _render_native_image(self, path: str) -> bool:
        """Emit *path* via the native protocol; True on success, False to fall back."""
        proto = self._image_protocol
        if proto is None:
            return False
        try:
            width = max(1, self._console.size.width - _RESULT_INDENT)
        except Exception:  # noqa: BLE001 — size probing must never break a turn
            width = 80
        seq = proto.encode(path, max_cols=width)
        if not seq:
            return False
        try:
            self._console.file.write(seq + "\n")
            self._console.file.flush()
        except Exception:  # noqa: BLE001 — a write failure degrades to half-block
            return False
        return True

    def _show_truncation(self, truncation: Truncation, *, spaces: int = _CONTENT_INDENT) -> None:
        """Print a dim ``⎿ +N lines`` / ``✂ full at <ref>`` footnote when folded.

        ``fold_note`` reads ``full_ref`` / ``hidden_lines`` straight off the
        :class:`Truncation` value object the reducer already extracted.
        """
        if not truncation.content_truncated:
            return
        self._console.print(self._indent(_fold_note(truncation), spaces))

    # ------------------------------------------------------------------
    # Incremental-Markdown live streaming
    # ------------------------------------------------------------------
    @staticmethod
    def _split_committable(text: str) -> tuple[str, str]:
        """Split into ``(finalized, pending)`` at the last safe block boundary."""
        lines = text.split("\n")
        fence = False
        last_boundary: Optional[int] = None
        for k in range(len(lines) - 1):
            line = lines[k]
            if line.strip().startswith("```"):
                fence = not fence
                continue
            if line == "" and not fence:
                last_boundary = k
        if last_boundary is None:
            return "", text
        return "\n".join(lines[:last_boundary]), "\n".join(lines[last_boundary + 1 :])

    def _tail(self, text: str) -> str:
        """Crop to the last few lines so the live region never fills the screen."""
        try:
            height = self._console.size.height or 24
        except Exception:  # noqa: BLE001 — size probing must never break a turn
            height = 24
        cap = max(1, height - 2)
        lines = text.split("\n")
        return "\n".join(lines[-cap:])

    def _commit(self, markdown_text: str) -> None:
        """Permanently print a finalized markdown block, opening the ``●`` turn."""
        md = themed_markdown(markdown_text)
        if self._assistant_open:
            self._console.print(self._indent(md, _CONTENT_INDENT))
        else:
            self._console.print()
            self._console.print(self._bullet_row(BULLET, md, style=Palette.BRAND))
            self._assistant_open = True

    def _show_tail(self) -> None:
        if not self._pending.strip():
            return
        tail = self._indent(themed_markdown(self._tail(self._pending)), _CONTENT_INDENT)
        if self._live is None:
            self._live = Live(
                tail,
                console=self._console,
                refresh_per_second=self._LIVE_REFRESH_PER_SECOND,
                vertical_overflow="crop",
                transient=True,
            )
            self._live.start()
        else:
            self._live.update(tail)

    def _stream(self, token: Any) -> None:
        text = token if isinstance(token, str) else str(token)
        if not text:
            return
        # A streamed token means the retry succeeded, so wipe any transient
        # countdown before opening the stream region (idempotent — the reducer
        # also sequences ``clear_retry`` ahead of a delta).
        self._clear_retry()
        self._pending += text
        finalized, remainder = self._split_committable(self._pending)
        if finalized.strip():
            self._pending = remainder
            if self._live is not None:
                self._live.update(self._indent(themed_markdown(self._tail(remainder)), _CONTENT_INDENT))
            self._commit(finalized)
        self._show_tail()

    def _end_stream(self) -> None:
        # Any event that finalizes/opens a block also ends the transient regions.
        self._clear_retry()
        self._clear_thinking()
        if self._live is None and not self._pending:
            self._assistant_open = False
            return
        live, self._live = self._live, None
        pending, self._pending = self._pending, ""
        try:
            if live is not None:
                live.stop()
        finally:
            if pending.strip():
                self._commit(pending)
        self._assistant_open = False

    def _clear_retry(self) -> None:
        """Erase the transient retry line (``stop()`` wipes a ``transient`` Live)."""
        if self._retry_live is not None:
            live, self._retry_live = self._retry_live, None
            live.stop()

    def _clear_thinking(self) -> None:
        """Erase the transient ``✻ 思考中`` indicator."""
        if self._thinking_live is not None:
            live, self._thinking_live = self._thinking_live, None
            live.stop()


__all__ = ["TerminalSurface"]
