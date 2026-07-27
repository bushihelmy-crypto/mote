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

import json
import re
from enum import Enum
from typing import Any, List, Optional, Tuple

from rich.style import Style

from mote.product.cli.consumers.render.builders._rich import Padding, Syntax, Table, Text, box
from mote.product.cli.consumers.render.builders.diff import render_diff
from mote.product.cli.consumers.render.palette import (
    BRANCH,
    BULLET,
    CHECK,
    COMPACT,
    CROSS,
    MEDIA,
    PLAY,
    PROMPT_SYMBOL,
    SCISSORS,
    SKIP,
    Palette,
)
from mote.product.cli.contracts.view import RESULT_KIND_DIFF, RESULT_KIND_TABLE
from mote.product.i18n import keys as K
from mote.product.i18n import t

# The stable ``ErrorCode`` string the permission gate stamps on a user-declined
# approval (mirrors ``ErrorCode.TOOL_PERMISSION_DENIED``). Kept as a literal so
# this render leaf never imports the exception package (leaf discipline — same
# reason the contract layer stores ``error_code`` as a bare string).
_REJECTION_CODE = "TOOL_PERMISSION_DENIED"

# Continuation indents (spaces) so wrapped/detail lines align under the glyph
# that introduces them: assistant/markdown under ``BULLET `` (2), a tool's
# result detail under ``  BRANCH `` (4).
CONTENT_INDENT = 2
RESULT_INDENT = 4

# The dim field separator between status-line parts (model │ tokens │ cost │ ctx),
# the ` │ ` vertical rule. Shared so both hosts divide the
# same way (terminal via ``format_usage_line``, Textual via ``StatusBar.render``).
USAGE_SEP = " \u2502 "


def notice_style(level: str) -> str:
    """Map a notice ``level`` to its palette style (warning/success, else dim).

    Shared by every rich host so an info/warning/success notice reads the same
    colour everywhere (§9.7 "format once").
    """
    return {"warning": Palette.WARNING, "success": Palette.SUCCESS}.get(level, Palette.DIM)


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


def is_rejection(ev: Any) -> bool:
    """True when a completed tool is the human *declining* an approval, not a failure.

    A permission-gate denial (``TOOL_PERMISSION_DENIED``) is a deliberate user
    choice — the tool never ran and nothing went wrong — so hosts render it as a
    muted "rejected" note (amber, no ``[ErrorType]`` suffix) rather than a red
    error row. Keyed on the stable ``error_code`` so a genuine ``PermissionError``
    raised *inside* a tool still reads as a real failure.
    """
    if ev is None or getattr(ev, "ok", True):
        return False
    return (getattr(ev, "error_code", "") or "") == _REJECTION_CODE


def tool_started_text(ev: Any, *, ok: Optional[bool] = None, blink: bool = False, rejected: bool = False) -> "Text":
    """Compose the ``● Tool(headline)`` invocation line (bullet added by caller).

    The bullet is coloured by run state (the status glyph): brand while
    the call is in flight (``ok is None``), success-green once it completes ok, and
    error-red when it fails. A ``rejected`` completion (the human declined the
    approval — see :func:`is_rejection`) uses the amber approval-gate colour
    instead of error-red, so a deliberate decline never reads as a failure. While
    running, ``blink`` (toggled by the host's heartbeat) brightens the bullet to
    :data:`Palette.SHIMMER` for a subtle pulse.
    """
    if ok is None:
        bullet_style = Palette.SHIMMER if blink else Palette.BRAND
    elif rejected:
        bullet_style = Palette.WARNING
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
    """Compose the ``  ⎿  summary`` result line, coloured by success.

    On a failure that carries a structured ``error_type`` (from the executor's
    ``ErrorReport``, projected onto the completion), a dim ``[ErrorType]`` suffix
    is appended — plus ``· retryable`` when the failure is retryable — so the
    human sees the machine-classified cause next to the summary. Absent
    ``error_type`` leaves the line unchanged.

    A user-declined approval (:func:`is_rejection`) is *not* a failure: it renders
    as a muted amber ``rejected`` note with no summary text and no ``[ErrorType]``
    suffix, so a deliberate decline never reads as a red error row.
    """
    if is_rejection(ev):
        line = Text()
        line.append("  " + BRANCH + " ", style=Palette.DIM)
        line.append(t(K.TOOL_REJECTED), style=Palette.WARNING)
        return line
    style = Palette.SUCCESS if ev.ok else Palette.ERROR
    summary = ev.summary or (t(K.RESULT_NO_OUTPUT) if ev.ok else t(K.RESULT_FAILED))
    line = Text()
    line.append("  " + BRANCH + " ", style=Palette.DIM)
    line.append(summary, style=style)
    error_type = getattr(ev, "error_type", "") or ""
    if not ev.ok and error_type:
        suffix = f" [{error_type}]"
        if getattr(ev, "retryable", False):
            suffix += f" · {t(K.RESULT_RETRYABLE)}"
        line.append(suffix, style=Palette.DIM)
    return line


def render_result_detail(ev: Any, spaces: int = RESULT_INDENT) -> List[Any]:
    """Render a completed tool's structured ``detail`` body, dispatched by kind.

    The projector already classified the result (§7.3); this is the single place
    both rich hosts turn that classification into renderables so the diff/table/
    plain look never drifts (§9.7 "format once"):

    * ``RESULT_KIND_DIFF`` → a +/- coloured unified diff.
    * ``RESULT_KIND_TABLE`` → a rich table from the TSV detail.
    * otherwise → a dimmed, URL-linkified plain preview.

    Returns a list of indented renderables (empty when there is no detail, or a
    table that failed to parse) so a host can ``print`` / mount each in order.
    """
    detail = getattr(ev, "detail", None)
    if not detail:
        return []
    kind = getattr(ev, "result_kind", None)
    if kind == RESULT_KIND_DIFF:
        return [indent(render_diff(detail), spaces)]
    if kind == RESULT_KIND_TABLE:
        table = build_table(detail)
        return [indent(table, spaces)] if table is not None else []
    return [indent(linkify(detail, base_style=Palette.DIM), spaces)]


def fold_note_str(ev: Any) -> str:
    """The plain-text fold hint under a truncated block — precise about *why*.

    Three honest states, most-specific first (the single source of truth for the
    fold wording so the rich :func:`fold_note` and the plain-text terminal host
    never drift):

    * ``full_ref`` — the framework hard-truncated an over-large result and
      persisted the whole thing to disk (``tool_result_limit``); point the human
      at that path with a ✂ scissors marker.
    * ``hidden_lines > 0`` — the projector dropped whole lines from the rendered
      detail; show the exact count ("… +N 行已折叠", a "+N lines" affordance).
    * otherwise — content was clipped in a way with no line count (a single long
      line word-capped, or a summary char-capped); a generic "内容已折叠".
    """
    full_ref = getattr(ev, "full_ref", None)
    hidden = getattr(ev, "hidden_lines", 0) or 0
    if full_ref:
        return f"{SCISSORS} " + t(K.FOLD_FULL_REF, ref=full_ref)
    if hidden > 0:
        return t(K.FOLD_HIDDEN_LINES, count=hidden)
    return t(K.FOLD_CONTENT)


def fold_note(ev: Any) -> "Text":
    """The one dim fold hint under a truncated block — precise about *why*.

    Wraps the shared :func:`fold_note_str` wording in a dim ``Text`` so every rich
    host draws the identical note; the plain-text terminal host reuses the string
    form directly (§9.7 "format once").
    """
    return Text(fold_note_str(ev), style=Palette.DIM)


def file_change_verb(old: str, new: str) -> str:
    """Classify a structured file change as ``created`` / ``deleted`` / ``updated``.

    Empty ``old`` means the file did not exist before → *created*; empty ``new``
    means it no longer exists → *deleted*; otherwise its contents changed →
    *updated*. Shared so both hosts (and the plain-text fallback) name the change
    identically.
    """
    return "created" if not old else ("deleted" if not new else "updated")


def file_change_caption(ev: Any) -> "Text":
    """The ``  ⎿  path (verb)`` caption above a structured file diff.

    Names the file and whether it was created/deleted/updated so the transcript
    records the change; shared by every rich host so the caption never drifts from
    the diff it introduces (§9.7 "format once").
    """
    old = getattr(ev, "old", "") or ""
    new = getattr(ev, "new", "") or ""
    path = getattr(ev, "path", "") or ""
    caption = Text()
    caption.append("  " + BRANCH + " ", style=Palette.DIM)
    caption.append(f"{path or 'file'} ", style=Palette.BRAND)
    caption.append(f"({file_change_verb(old, new)})", style=Palette.DIM)
    return caption


def media_caption(ev: Any) -> "Text":
    """The ``  ⎿  ⧉ [kind] ref`` caption line for a media artifact.

    Always drawn so the transcript records *what* was shown even when the image
    itself can't be painted inline (a text-only host, an unreadable ref); shared
    by every rich host so the label reads identically.
    """
    label = getattr(ev, "media_kind", None) or "media"
    ref = getattr(ev, "ref", None) or getattr(ev, "alt", None) or "(no reference)"
    caption = Text()
    caption.append("  " + BRANCH + " ", style=Palette.DIM)
    caption.append(f"{MEDIA} [{label}] ", style=Palette.BRAND)
    caption.append(ref, style=Palette.DIM)
    return caption


# The background-task status glyph + colour, keyed by ``TaskProgress.status``.
# An unknown status falls back to the dim ``⊘`` skip marker.
_TASK_PROGRESS_STYLE = {
    "running": (PLAY, Palette.BRAND),
    "success": (CHECK, Palette.SUCCESS),
    "failed": (CROSS, Palette.ERROR),
}


def task_progress_text(ev: Any) -> "Text":
    """The ``  ▶ stage status`` background-task progress line.

    The glyph + colour track the run state (running/success/failed → play/check/
    cross, else a dim skip); a ``failed`` line appends its ``detail``. Shared so
    both hosts render task progress identically (§9.7 "format once").
    """
    status = getattr(ev, "status", "")
    stage = getattr(ev, "stage", "") or "?"
    detail = getattr(ev, "detail", "")
    symbol, style = _TASK_PROGRESS_STYLE.get(status, (SKIP, Palette.WARNING))
    line = Text()
    line.append("  " + symbol + " ", style=style)
    line.append(f"{stage} {status}", style=style)
    if detail and status == "failed":
        line.append(f": {detail}", style=Palette.DIM)
    return line


# --- tool-row fold classification (the single source of truth) ------------
# How a tool's transcript row folds under the global ctrl+o toggle. Two GENUINELY
# different behaviours (not one thing with a flag), plus "never folds":
#
#   GROUP  — coalesce a *run* of consecutive calls into ONE count summary line
#            ("搜索 2 个模式，读取 1 个文件"), a collapsed read/search summary.
#            The individual call's identity doesn't matter (search = Grep/Glob,
#            read = Read); a non-folding tool or assistant text breaks the run.
#   DETAIL — fold *this* call's body+output behind its ``● Tool(headline)`` line.
#            Each command's identity matters, so the calls are NOT merged — every
#            call keeps its own row. This is the DEFAULT for any tool that is
#            neither a GROUP tool nor on the NONE deny-list below.
#   NONE   — always rendered in full (Edit/Write): the "what changed" is the
#            point, so these stay expanded and ctrl+o does not fold them.
#
# The DETAIL default is a deny-list (``_FOLD_NONE``), not an allow-list: any new
# tool is foldable by default, and only the few tools whose full body should
# always show are opted out.
#
# This classifier is a pure, host-agnostic builder so both rich hosts (and a
# future terminal adoption) share one definition; the search/read *count*
# phrasing below keeps the Search vs Read split for the summary.
_GROUP_SEARCH = {"Search"}
_GROUP_READ = {"Read"}
_FOLD_NONE = {"Edit"}


class FoldMode(Enum):
    """How a tool row folds under ctrl+o — see the classifier notes above."""

    NONE = "none"
    GROUP = "group"
    DETAIL = "detail"


def fold_mode(name: str) -> FoldMode:
    """Classify how tool *name*'s transcript row folds (the single source of truth)."""
    if name in _GROUP_SEARCH or name in _GROUP_READ:
        return FoldMode.GROUP
    if name in _FOLD_NONE:
        return FoldMode.NONE
    return FoldMode.DETAIL


def tool_group_summary_text(items: Any, *, active: bool) -> "Text":
    """The one-line ``● 搜索 N 个模式，读取 M 个文件`` summary of a collapsed group.

    *items* is a list of ``(tool_name, path)`` tuples (one per grouped tool call).
    The **search** count is the number of Grep/Glob calls; the **read** count is
    the number of *unique* Read file paths — a repeat of the same path counts once
    (a set-based dedupe), while a path-less Read counts
    individually. ``active`` (any tool still in flight) appends a progressive
    ``…``. The ctrl+o toggle affordance is a single unified hint on the status
    bar, not repeated on each row. Empty *items* (or no search/read among them)
    → empty ``Text`` (nothing to show).
    """
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
        parts.append(t(K.GROUP_SEARCH, count=search))
    if read:
        parts.append(t(K.GROUP_READ, count=read))
    if not parts:
        return text
    text.append(BULLET + " ", style=Palette.BRAND)
    text.append(t(K.LIST_SEP).join(parts), style=Palette.DIM)
    if active:
        text.append("…", style=Palette.DIM)
    return text


def conversation_compacted_text(ev: Any) -> "Text":
    """The dim ``✻ 对话已压缩`` boundary marker (the compacted line).

    Marker-only by design (the summary body stays in the model's context, not the
    human transcript): shows *that* history was condensed and, when known, how
    many messages the rebuilt history holds.
    """
    count = getattr(ev, "message_count", 0) or 0
    line = Text()
    line.append(f"{COMPACT} " + t(K.COMPACT_COMPACTED), style=Palette.DIM)
    if count:
        line.append(f" ({t(K.COMPACT_KEPT, count=count)})", style=Palette.DIM)
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
        text.append("\n" + t(K.FOLD_MORE_LINES, count=hidden), style=Palette.DIM)
    return text


# --- activity (nested orchestration) topology + outcome -------------------
# A ``run_graph`` (and, later, a sub-agent / background task) is a nested
# orchestration; these two builders turn its neutral, pre-computed topology /
# node-state structures (plain dicts — the contract layer imports nothing from
# bggraph) into the shared look both rich hosts land. ``activity_topology`` draws
# the declared shape once at open; ``activity_outcome`` draws the final per-node
# result tree at close (self-sufficient — read straight off the terminal event,
# never the live stream, so a replayed transcript renders identically).

# Per-node-kind marker for the topology view — a glyph that hints how the node
# runs (a plain tool call, a parallel fan-out, a serial fold, a pure compute).
_NODE_KIND_GLYPH = {
    "tool": "\u25c6",  # ◆ — a single tool call
    "map": "\u21c9",  # ⇉ — parallel fan-out over a collection
    "fold": "\u2192",  # → — serial accumulate
    "compute": "\u0192",  # ƒ — pure data-shaping
}

# Per-node terminal status → (glyph, style) for the outcome tree. Mirrors the
# task-progress mapping but keyed on a graph node's final ``BgStatus`` string;
# an unknown status falls back to the dim skip marker.
_NODE_STATUS_STYLE = {
    "success": (CHECK, Palette.SUCCESS),
    "failed": (CROSS, Palette.ERROR),
    "skipped": (SKIP, Palette.DIM),
    "cancelled": (SKIP, Palette.WARNING),
    "running": (PLAY, Palette.BRAND),
    "pending": (SKIP, Palette.DIM),
}


def activity_header(activity_kind: str, label: str) -> "Text":
    """The ``  ⎿ label (kind)`` heading line shared by topology + outcome.

    A ``run_graph`` orchestration already has a ``● RunGraph`` tool-call row
    above it, so the activity block does NOT draw a second top-level ``●``
    bullet — it hangs off that row as a nested ``⎿`` branch (offset/indented),
    the same tree affordance a tool's result detail uses.
    """
    line = Text()
    line.append("  " + BRANCH + " ", style=Palette.DIM)
    line.append(label or activity_kind or "activity", style=f"bold {Palette.BRAND}")
    if activity_kind and activity_kind != (label or ""):
        line.append(f" ({activity_kind})", style=Palette.DIM)
    return line


def activity_topology(activity_kind: str, label: str, topology: Any) -> "Text":
    """Render a nested orchestration's declared shape (nodes + guarded edges).

    *topology* is the neutral pre-computed structure the projector carried:
    ``{"nodes": [{"id", "kind", "label"}...], "edges": [{"from", "to",
    "guarded": bool}...]}``. Each node is one indented ``  <glyph> label`` line
    (the glyph hints its kind); guarded edges are appended as a dim ``when``
    annotation so the branch/loop structure is visible. Missing/empty topology
    degrades to just the header (nothing to draw).
    """
    text = activity_header(activity_kind, label)
    topo = topology or {}
    nodes = topo.get("nodes") or []
    for node in nodes:
        nid = node.get("id", "") or ""
        nkind = node.get("kind", "") or ""
        nlabel = node.get("label", "") or nid
        glyph = _NODE_KIND_GLYPH.get(nkind, "\u2022")  # • fallback
        text.append("\n")
        text.append(f"    {glyph} ", style=Palette.DIM)
        text.append(nlabel, style=Palette.BRAND)
        if nkind:
            text.append(f" [{nkind}]", style=Palette.DIM)
    edges = topo.get("edges") or []
    guarded = [e for e in edges if e.get("guarded")]
    for e in guarded:
        text.append("\n")
        text.append(f"      {BRANCH} ", style=Palette.DIM)
        text.append(f"{e.get('from', '?')} \u2192 {e.get('to', '?')}", style=Palette.DIM)
        text.append(" (when)", style=Palette.DIM)
    return text


def _node_retry_args(node: Any) -> str:
    """Bounded JSON of a failed node's ``args`` so the model can retry the call."""
    args = node.get("args")
    if not args:
        return ""
    try:
        txt = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        txt = repr(args)
    if len(txt) > 200:
        txt = txt[:200] + "\u2026"
    return txt


def activity_outcome(node_states: Any, outcome: str, summary: str) -> "Text":
    """Render a nested orchestration's final per-node result tree.

    *node_states* is the neutral list the terminal event carried:
    ``[{"id", "kind", "label", "status", "attempts", "error", "args"}...]``.
    The head is a nested ``  ⎿ <✓/✗> outcome`` branch (no second ``●`` bullet —
    it hangs off the ``● RunGraph`` tool-call row above it); each node is an
    indented ``    <✓/⊘/✗> label`` line coloured by its final status, with the
    attempt count when it retried; a failed node appends its error and the
    ``args`` needed to retry that exact call (reusing ``run_graph``'s bounded
    retry-note format). A trailing ``summary`` line, when present, is dimmed.
    """
    line = Text()
    ok = (outcome or "success") == "success"
    head_glyph, head_style = (CHECK, Palette.SUCCESS) if ok else (CROSS, Palette.ERROR)

    line.append("  " + BRANCH + " ", style=Palette.DIM)
    line.append(f"{head_glyph} {outcome or 'success'}", style=head_style)
    for node in node_states or ():
        nid = node.get("id", "") or ""
        nlabel = node.get("label", "") or nid
        status = node.get("status", "") or ""
        attempts = int(node.get("attempts", 0) or 0)
        glyph, style = _NODE_STATUS_STYLE.get(status, (SKIP, Palette.DIM))
        line.append("\n")
        line.append(f"    {glyph} ", style=style)
        line.append(nlabel, style=style)
        if attempts > 1:
            line.append(f" (\u00d7{attempts})", style=Palette.DIM)  # ×N attempts
        if status == "failed":
            err = node.get("error", "") or ""
            if err:
                line.append(f": {err}", style=Palette.DIM)
            args_txt = _node_retry_args(node)
            if args_txt:
                line.append("\n")
                line.append(f"      {BRANCH} {args_txt}", style=Palette.DIM)
    if summary and (summary or "").strip():
        line.append("\n")
        line.append(f"    {summary.strip()}", style=Palette.DIM)
    return line


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
    # ``│`` (U+2502) field separator — the status-line look (dim in
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
    "is_rejection",
    "FoldMode",
    "fold_mode",
    "tool_group_summary_text",
    "notice_style",
    "render_result_detail",
    "fold_note",
    "fold_note_str",
    "file_change_verb",
    "file_change_caption",
    "media_caption",
    "task_progress_text",
    "activity_header",
    "activity_topology",
    "activity_outcome",
    "conversation_compacted_text",
    "compaction_summary_text",
    "session_table",
    "format_usage_line",
]
