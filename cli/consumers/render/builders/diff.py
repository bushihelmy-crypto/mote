#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Console-free unified-diff colouriser (claude-code style).

Two entry points share the same parse → word-level pair → render pipeline:
:func:`render_diff` (from raw diff *text* a shell produced) and
:func:`render_file_change` (from the structured ``old``/``new`` fact of an Edit /
apply_patch). Both return a coloured ``rich.Text`` with a line-number gutter,
filled +/- bars, cyan hunk headers, and word-level highlight of the exact spans
that changed within a matched -/+ pair.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher, unified_diff
from typing import List, Tuple

from metagpt.cli.consumers.render.builders._rich import Text
from metagpt.cli.consumers.render.palette import Palette

# Cap the filled-bar width so a single very long line can't force absurd padding
# on every other bar (rich still folds anything past the console width anyway).
_DIFF_BAR_MAX = 200

# Split a line into word-ish tokens (runs of whitespace or non-whitespace) so the
# intra-line highlight lands on whole words, not scattered characters.
_TOKEN_RE = re.compile(r"\s+|\S+")
# Parse a unified-diff hunk header: ``@@ -oldStart[,oldLen] +newStart[,newLen] @@``.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class _Row:
    """One parsed diff line: its kind, resolved line numbers, and word segments."""

    __slots__ = ("kind", "old", "new", "content", "raw", "segments")

    def __init__(self, kind: str, old, new, content: str, raw: str) -> None:
        self.kind = kind  # add | del | ctx | hunk | meta
        self.old = old  # old-side line number (or None)
        self.new = new  # new-side line number (or None)
        self.content = content  # line body (marker stripped)
        self.raw = raw  # original line (for hunk / meta)
        # List of (text, emphasized) — the whole content unchanged by default;
        # word-level pairing (below) overrides this for matched -/+ rows.
        self.segments: List[Tuple[str, bool]] = [(content, False)] if content else []


def _parse_diff_rows(lines: List[str]) -> List[_Row]:
    """Fold raw diff lines into ``_Row``s, resolving old/new line numbers.

    Line numbering only begins once a ``@@`` hunk header is seen (before that,
    numbers stay blank) so a headerless ``--- / +++`` preamble never invents
    bogus numbers.
    """
    rows: List[_Row] = []
    old = new = None  # None until the first hunk header anchors the counters
    for ln in lines:
        m = _HUNK_RE.match(ln)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            rows.append(_Row("hunk", None, None, "", ln))
        elif ln.startswith("+++") or ln.startswith("---"):
            rows.append(_Row("meta", None, None, "", ln))
        elif ln.startswith("+"):
            rows.append(_Row("add", None, new, ln[1:], ln))
            new = new + 1 if new is not None else None
        elif ln.startswith("-"):
            rows.append(_Row("del", old, None, ln[1:], ln))
            old = old + 1 if old is not None else None
        elif ln.startswith(" ") or ln == "":
            body = ln[1:] if ln else ""
            rows.append(_Row("ctx", old, new, body, ln))
            if old is not None:
                old += 1
            if new is not None:
                new += 1
        else:  # a stray non-diff line (\ No newline at end of file, etc.)
            rows.append(_Row("meta", None, None, "", ln))
    return rows


def _word_segments(a: str, b: str) -> Tuple[List[Tuple[str, bool]], List[Tuple[str, bool]]]:
    """Word-level diff of two lines → (del_segments, add_segments).

    Each segment is ``(text, emphasized)``; equal runs are unemphasized, changed
    runs (replace/insert/delete) are emphasized so only the true delta lights up.
    """
    a_toks, b_toks = _TOKEN_RE.findall(a), _TOKEN_RE.findall(b)
    a_segs: List[Tuple[str, bool]] = []
    b_segs: List[Tuple[str, bool]] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=a_toks, b=b_toks).get_opcodes():
        a_text, b_text = "".join(a_toks[i1:i2]), "".join(b_toks[j1:j2])
        emph = tag != "equal"
        if a_text:
            a_segs.append((a_text, emph))
        if b_text:
            b_segs.append((b_text, emph))
    return a_segs or [("", False)], b_segs or [("", False)]


def _pair_word_level(rows: List[_Row]) -> None:
    """Attach word-level segments to each -/+ pair (a contiguous del-run then add-run)."""
    i = 0
    n = len(rows)
    while i < n:
        if rows[i].kind != "del":
            i += 1
            continue
        j = i
        while j < n and rows[j].kind == "del":
            j += 1
        k = j
        while k < n and rows[k].kind == "add":
            k += 1
        dels, adds = rows[i:j], rows[j:k]
        for d_row, a_row in zip(dels, adds):
            d_segs, a_segs = _word_segments(d_row.content, a_row.content)
            d_row.segments, a_row.segments = d_segs, a_segs
        i = k if k > i else i + 1


def render_diff(diff_text: str) -> "Text":
    """Colour a unified diff claude-code-style — the *text* entry point.

    For a tool that only produced diff **text** (a shell ``git diff``, ``diff -u``
    — no structured old/new available): parse the unified diff and colourise it.
    Filled +/- bars, a line-number gutter, ``@@`` hunk headers in cyan, and
    word-level highlight of the exact spans that changed within a matched -/+ pair.

    When the change is a *structured fact* (``old``/``new`` full content from Edit /
    apply_patch), prefer :func:`render_file_change` — it owns the full facts and a
    rich host can drive an interactive side-by-side from them, not just this diff.
    """
    rows = _parse_diff_rows(diff_text.splitlines())
    _pair_word_level(rows)
    return _render_rows(rows)


def render_file_change(old: str, new: str, path: str = "") -> "Text":
    """Colour a file change from its *structured fact* — the ``old``/``new`` entry.

    The honest counterpart to :func:`render_diff`: the change is the pair of full
    contents (a display-agnostic fact), not pre-formatted diff text. We synthesize
    a unified diff from ``old``/``new`` here **only** because this text host renders
    a coloured diff; a media-capable host (Web) consumes the same ``old``/``new``
    off the ``FileDiffBlock`` and drives an interactive side-by-side instead — the
    fact travels, the diff synthesis is this host's private display choice.

    ``path`` labels the ``--- / +++`` header (cosmetic). A creation (``old==""``)
    and a deletion (``new==""``) both fall out naturally from difflib.
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    label = path or "file"
    diff_lines = list(
        unified_diff(old_lines, new_lines, fromfile=label, tofile=label, lineterm="")
    )
    rows = _parse_diff_rows(diff_lines)
    _pair_word_level(rows)
    return _render_rows(rows)


def _render_rows(rows: List[_Row]) -> "Text":
    """Render parsed ``_Row``s to a coloured ``Text`` (shared by both diff entries)."""
    # Gutter widths from the widest resolved line number on each side.
    w_old = max((len(str(r.old)) for r in rows if r.old is not None), default=1)
    w_new = max((len(str(r.new)) for r in rows if r.new is not None), default=1)
    # Common bar width so add/del blocks share a right edge (marker + space + body).
    bar_w = min(
        _DIFF_BAR_MAX,
        max((2 + len(r.content) for r in rows if r.kind in ("add", "del")), default=0),
    )

    def _gutter(old, new) -> str:
        old_s = str(old) if old is not None else ""
        new_s = str(new) if new is not None else ""
        return f"{old_s:>{w_old}} {new_s:>{w_new}} "

    out = Text()
    for idx, r in enumerate(rows):
        if idx:
            out.append("\n")
        if r.kind == "hunk":
            out.append(_gutter(None, None), style=Palette.DIM)
            out.append(r.raw, style=f"bold {Palette.DIFF_HUNK}")
            continue
        if r.kind == "meta":
            out.append(r.raw, style=Palette.DIM)
            continue
        if r.kind == "ctx":
            out.append(_gutter(r.old, r.new), style=Palette.DIM)
            out.append("  " + r.content, style=Palette.DIM)
            continue
        # add / del: a filled, word-highlighted bar.
        add = r.kind == "add"
        fg = Palette.DIFF_ADD if add else Palette.DIFF_DEL
        base_bg = Palette.DIFF_ADD_BG if add else Palette.DIFF_DEL_BG
        emph_bg = Palette.DIFF_ADD_EMPH_BG if add else Palette.DIFF_DEL_EMPH_BG
        out.append(_gutter(r.old, r.new), style=Palette.DIM)
        out.append(("+ " if add else "- "), style=f"bold {fg} on {base_bg}")
        used = 2
        for text, emph in r.segments:
            style = f"bold {fg} on {emph_bg}" if emph else f"{fg} on {base_bg}"
            out.append(text, style=style)
            used += len(text)
        if bar_w > used:  # pad the bar so blocks align to a common right edge
            out.append(" " * (bar_w - used), style=f"{fg} on {base_bg}")
    return out


__all__ = ["render_diff", "render_file_change"]
