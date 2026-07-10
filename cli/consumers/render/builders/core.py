#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Core console-free rich builders shared by every rich host.

The renderables here turn a ``ViewEvent`` (or raw string) into a ``rich``
renderable with no ``Console`` and no side effects, so any host (the scrolling
terminal *and* the full-screen Textual TUI) reuses the exact same look
(§9.7 "format once"): layout primitives (``bullet_row``/``indent``), the tool
headline/summary/fold lines, the collapsed search/read group summary, the
compaction markers, the session table and the usage line.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from metagpt.cli.consumers.render.builders._rich import Padding, Syntax, Table, Text, box
from metagpt.cli.consumers.render.palette import (
    BRANCH,
    Palette,
)

# Continuation indents (spaces) so wrapped/detail lines align under the glyph
# that introduces them: assistant/markdown under ``BULLET `` (2), a tool's
# result detail under ``  BRANCH `` (4).
CONTENT_INDENT = 2
RESULT_INDENT = 4

# The dim field separator between status-line parts (model │ tokens │ cost │ ctx),
# mirroring claude-code's ` │ ` vertical rule. Shared so both hosts divide the
# same way (terminal via ``format_usage_line``, Textual via ``StatusBar.render``).
USAGE_SEP = " \u2502 "


def bullet_row(glyph: str, renderable: Any, *, style: str) -> Any:
    """A two-column ``glyph | renderable`` grid (glyph at col 0, content at 2)."""
    grid = Table.grid(padding=(0, 0))
    grid.add_column(no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(Text(glyph + " ", style=style), renderable)
    return grid


def indent(renderable: Any, spaces: int = CONTENT_INDENT) -> Any:
    """Left-pad a renderable so continuation lines sit under the content column."""
    return Padding(renderable, (0, 0, 0, spaces))


def build_table(tsv: str) -> Optional["Table"]:
    """Build a rich table from TSV ``detail`` (first row = header)."""
    rows = [line.split("\t") for line in tsv.splitlines() if line.strip()]
    if not rows:
        return None
    table = Table(show_header=True, header_style=f"bold {Palette.BRAND}", box=box.SIMPLE)
    for col in rows[0]:
        table.add_column(col)
    for row in rows[1:]:
        table.add_row(*row)
    return table


def tool_body_syntax(ev: Any) -> Optional["Syntax"]:
    """Highlight a tool's ``body`` with its ``lexer`` (``None`` when no body)."""
    if not getattr(ev, "body", None):
        return None
    return Syntax(
        ev.body,
        ev.lexer or "text",
        theme="ansi_dark",
        word_wrap=True,
        background_color="default",  # no heavy filled box; blend with terminal
    )


def user_message_row(text: str) -> Any:
    """Compose the user's own message as a ``❯ text`` transcript block.

    Rendered with the prompt chevron (brand-coloured) so the human's turn is
    visually distinct from the assistant's ``●`` bullet; the text is *literal*
    (not markdown) since it is exactly what the user typed. Both rich hosts share
    this one builder so the user's message looks identical everywhere.
    """
    from metagpt.cli.consumers.render.palette import PROMPT_SYMBOL

    return bullet_row(PROMPT_SYMBOL, Text(text), style=f"bold {Palette.BRAND}")


# A URL match: http/https up to the first whitespace, with common trailing
# punctuation (``.,;:!?`` and a closing bracket/quote) trimmed off the tail so a
# sentence like ``see https://ex.com.`` links ``https://ex.com`` (not the period).
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_URL_TRAILING = ")]}>,.;:!?\"'`"


def _split_url(match: str) -> Tuple[str, str]:
    """Trim trailing sentence punctuation from *match* → (url, trailing)."""
    trailing = ""
    while match and match[-1] in _URL_TRAILING:
        # Keep a closing paren that balances an opening one inside the URL
        # (e.g. a wiki link ``…_(disambiguation)``); only trim it when unbalanced
        # (more ``)`` than ``(``), where the last one is really sentence punctuation.
        if match[-1] == ")" and match.count(")") <= match.count("("):
            break
        trailing = match[-1] + trailing
        match = match[:-1]
    return match, trailing


def linkify(text: str, *, base_style: str = "") -> "Text":
    """Turn bare ``http(s)://`` URLs in *text* into clickable link spans.

    Each URL span carries ``Style(link=url)`` — rich emits it as an OSC 8
    hyperlink on a real terminal (so the terminal opens it on Ctrl+click), and
    Textual preserves ``.link`` on the span so the app's Ctrl+click handler can
    read and open it. Everything outside a URL keeps *base_style*. When ``rich``
    is unavailable the caller degrades to plain text elsewhere; here we still
    return a ``Text`` so the shape is stable.
    """
    from rich.style import Style

    out = Text()
    idx = 0
    for m in _URL_RE.finditer(text):
        start, end = m.span()
        if start > idx:
            out.append(text[idx:start], style=base_style)
        url, trailing = _split_url(m.group())
        out.append(url, style=Style.parse(f"underline {Palette.LINK}") + Style(link=url))
        if trailing:
            out.append(trailing, style=base_style)
        idx = end
    if idx < len(text):
        out.append(text[idx:], style=base_style)
    return out


def tool_started_text(ev: Any, *, ok: Optional[bool] = None, blink: bool = False) -> "Text":
    """Compose the ``● Tool(headline)`` invocation line (bullet added by caller).

    The bullet is coloured by run state (claude-code's status glyph): brand while
    the call is in flight (``ok is None``), success-green once it completes ok, and
    error-red when it fails. While running, ``blink`` (toggled by the host's
    heartbeat) brightens the bullet to :data:`Palette.SHIMMER` for a subtle pulse.
    """
    from metagpt.cli.consumers.render.palette import BULLET

    if ok is None:
        bullet_style = Palette.SHIMMER if blink else Palette.BRAND
    else:
        bullet_style = Palette.SUCCESS if ok else Palette.ERROR
    line = Text()
    line.append(BULLET + " ", style=bullet_style)
    line.append(ev.title or ev.tool_name, style=f"bold {Palette.BRAND}")
    if ev.headline:
        line.append("(", style=Palette.DIM)
        line.append(ev.headline, style=Palette.DIM)
        line.append(")", style=Palette.DIM)
    return line


def tool_completed_text(ev: Any) -> "Text":
    """Compose the ``  ⎿  summary`` result line, coloured by success."""
    style = Palette.SUCCESS if ev.ok else Palette.ERROR
    summary = ev.summary or ("(no output)" if ev.ok else "failed")
    line = Text()
    line.append("  " + BRANCH + " ", style=Palette.DIM)
    line.append(summary, style=style)
    return line


def fold_note(ev: Any) -> "Text":
    """The one dim fold hint under a truncated block — precise about *why*.

    Three honest states, most-specific first (shared by every rich host so the
    wording never drifts):

    * ``full_ref`` — the framework hard-truncated an over-large result and
      persisted the whole thing to disk (``tool_result_limit``); point the human
      at that path with a ✂ scissors marker.
    * ``hidden_lines > 0`` — the projector dropped whole lines from the rendered
      detail; show the exact count ("… +N 行已折叠", claude-code's "+N lines").
    * otherwise — content was clipped in a way with no line count (a single long
      line word-capped, or a summary char-capped); a generic "内容已折叠".
    """
    from metagpt.cli.consumers.render.palette import SCISSORS

    full_ref = getattr(ev, "full_ref", None)
    hidden = getattr(ev, "hidden_lines", 0) or 0
    line = Text()
    if full_ref:
        line.append(f"{SCISSORS} 输出过大已截断，完整见 {full_ref}", style=Palette.DIM)
    elif hidden > 0:
        line.append(f"… +{hidden} 行已折叠", style=Palette.DIM)
    else:
        line.append("… 内容已折叠", style=Palette.DIM)
    return line


# --- consecutive search/read tool grouping (claude-code collapseReadSearch) ---
# claude-code coalesces a *run* of consecutive collapsible tool calls into one
# line ("Searched for 2 patterns, read 1 file"), expandable with ctrl+o; a
# non-collapsible tool (Edit/Write) or assistant text breaks the run. The grouped
# tools are search = Grep/Glob (a pattern query) and read = Read (a file). We keep
# the classification + count phrasing HERE as a pure, host-agnostic builder so the
# grouping look never diverges and a future terminal adoption is a closed change.
_GROUP_SEARCH = {"Grep", "Glob"}
_GROUP_READ = {"Read"}


def is_collapsible_tool(name: str) -> bool:
    """Whether *name* is a search/read tool that coalesces into a group."""
    return name in _GROUP_SEARCH or name in _GROUP_READ


def tool_group_summary_text(items: Any, *, active: bool, expanded: bool) -> "Text":
    """The one-line ``● 搜索 N 个模式，读取 M 个文件`` summary of a collapsed group.

    *items* is a list of ``(tool_name, path)`` tuples (one per grouped tool call).
    The **search** count is the number of Grep/Glob calls; the **read** count is
    the number of *unique* Read file paths — a repeat of the same path counts once
    (mirroring claude-code's ``Set`` dedupe), while a path-less Read counts
    individually. ``active`` (any tool still in flight) appends a progressive
    ``…``; a dim ``(ctrl+o 展开/折叠)`` hint tells the human the row toggles. Empty
    *items* (or no search/read among them) → empty ``Text`` (nothing to show).
    """
    from metagpt.cli.consumers.render.palette import BULLET

    text = Text()
    if not items:
        return text
    search = sum(1 for name, _ in items if name in _GROUP_SEARCH)
    read_paths: set = set()
    read_pathless = 0
    for name, path in items:
        if name in _GROUP_READ:
            if path:
                read_paths.add(path)
            else:
                read_pathless += 1
    read = len(read_paths) + read_pathless
    parts: List[str] = []
    if search:
        parts.append(f"搜索 {search} 个模式")
    if read:
        parts.append(f"读取 {read} 个文件")
    if not parts:
        return text
    text.append(BULLET + " ", style=Palette.BRAND)
    text.append("，".join(parts), style=Palette.DIM)
    if active:
        text.append("…", style=Palette.DIM)
    text.append(f" (ctrl+o {'折叠' if expanded else '展开'})", style=Palette.DIM)
    return text


def conversation_compacted_text(ev: Any) -> "Text":
    """The dim ``✻ 对话已压缩`` boundary marker (claude-code's compacted line).

    Marker-only by design (the summary body stays in the model's context, not the
    human transcript): shows *that* history was condensed and, when known, how
    many messages the rebuilt history holds.
    """
    from metagpt.cli.consumers.render.palette import COMPACT

    count = getattr(ev, "message_count", 0) or 0
    line = Text()
    line.append(f"{COMPACT} 对话已压缩", style=Palette.DIM)
    if count:
        line.append(f" (保留 {count} 条消息)", style=Palette.DIM)
    return line


# How many summary lines to keep on-screen after a compaction clear before
# folding the tail into a "+N 行" hint (the recap can be long; this keeps the
# re-rendered compaction header compact while still bridging to prior context).
_COMPACT_SUMMARY_MAX_LINES = 12


def compaction_summary_text(summary: str, *, max_lines: int = _COMPACT_SUMMARY_MAX_LINES) -> "Text":
    """The dim, folded compaction *recap* re-rendered after a full-screen clear.

    A full-screen host (Textual) has no native scrollback, so on compaction it
    wipes the now-stale transcript; the engine's ``summary`` is then the only
    on-screen bridge to what came before. We show it dim and fold past
    *max_lines* with a "… +N 行" tail so a long recap doesn't dominate the fresh
    screen. Empty summary → an empty ``Text`` (the caller skips mounting it).
    """
    text = Text()
    lines = (summary or "").strip().splitlines()
    if not lines:
        return text
    shown = lines[:max_lines]
    for i, ln in enumerate(shown):
        if i:
            text.append("\n")
        text.append(ln, style=Palette.DIM)
    hidden = len(lines) - len(shown)
    if hidden > 0:
        text.append(f"\n… +{hidden} 行", style=Palette.DIM)
    return text


def session_table(ev: Any) -> Optional["Table"]:
    """Build the numbered resumable-session table from a ``SessionListShown`` event."""
    if not getattr(ev, "items", None):
        return None
    table = Table(
        title=getattr(ev, "title", "Sessions"),
        show_header=True,
        header_style=f"bold {Palette.BRAND}",
        box=box.SIMPLE,
    )
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
    return table


def format_usage_line(ev: Any) -> str:
    """Compose a compact one-line usage summary from an ``UsageUpdated`` event.

    Every figure is already pre-computed by the projector (``context_pct`` is a
    0-1 ratio, not a division the consumer performs). Only the parts that are
    present render — a host with partial usage data still gets a clean line.
    """
    parts: list[str] = []
    if ev.model:
        parts.append(str(ev.model))
    if ev.total_tokens:
        parts.append(f"{ev.total_tokens:,} tok")
    elif ev.input_tokens or ev.output_tokens:
        parts.append(f"{ev.input_tokens:,}→{ev.output_tokens:,} tok")
    if ev.cost_usd is not None:
        parts.append(f"${ev.cost_usd:.4f}")
    if ev.context_pct is not None:
        parts.append(f"ctx {ev.context_pct * 100:.0f}%")
    elif ev.context_used is not None and ev.context_window:
        parts.append(f"ctx {ev.context_used:,}/{ev.context_window:,}")
    # ``│`` (U+2502) field separator — claude-code's status-line look (dim in
    # both hosts). A vertical rule reads as a divider far better than spaces.
    return USAGE_SEP.join(parts)


__all__ = [
    "CONTENT_INDENT",
    "RESULT_INDENT",
    "USAGE_SEP",
    "bullet_row",
    "indent",
    "build_table",
    "tool_body_syntax",
    "user_message_row",
    "linkify",
    "tool_started_text",
    "tool_completed_text",
    "is_collapsible_tool",
    "tool_group_summary_text",
    "fold_note",
    "conversation_compacted_text",
    "compaction_summary_text",
    "session_table",
    "format_usage_line",
]
