#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Per-tool result summaries ("Read N lines" / "Found N files" …).

The projector computes a count-based one-liner ONCE per tool (窄腰) so a human
reads a meaningful chrome line ("读取 42 行") instead of the raw first line of
output (e.g. Read's ``     1→import os``). Chinese wording per the CLI's
convention. :func:`_result_summary` returns ``None`` for tools/shapes with no
honest count → the caller falls back to the first-non-empty-line summary
(Bash/terminal/unknown keep their raw output line).

Pure functions only (``(name, event, text) → Optional[str]``): no I/O, no
projector state. Split out of ``projector.py`` so the fold's main class stays
focused on the ``AgentEvent → ViewEvent`` fold itself.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from mote.common.i18n import keys as K
from mote.common.i18n import t

# A Read body line: right-justified number + ``→`` (U+2192) + content.
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\u2192")
# Search ``count`` mode summary: "Found N total occurrences across M files".
_GREP_COUNT_RE = re.compile(r"Found (\d+) total occurrences? across (\d+) files?")
# Search ``files_with_matches`` header: "Found N file(s)".
_GREP_FILES_RE = re.compile(r"^Found (\d+) files?")
# A search's trailing non-path note emitted at the result cap.
_GLOB_TRUNC = "(Results are truncated"


def _count_numbered_lines(text: str) -> int:
    return sum(1 for ln in text.splitlines() if _NUMBERED_LINE_RE.match(ln))


def _diff_counts(old: str, new: str) -> tuple[int, int]:
    """``(added, removed)`` line counts between two file versions (Edit chrome)."""
    from difflib import SequenceMatcher

    added = removed = 0
    matcher = SequenceMatcher(None, old.splitlines(), new.splitlines())
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return added, removed


def _summary_read(text: str) -> Optional[str]:
    head = text.lstrip()
    if head.startswith("Read image"):
        return t(K.SUMMARY_READ_IMAGE)
    if head.startswith("Read PDF"):
        return t(K.SUMMARY_READ_PDF)
    n = _count_numbered_lines(text)
    return t(K.SUMMARY_READ_LINES, count=n) if n else None


def _summary_search(text: str) -> Optional[str]:
    """Count summary for the unified Search tool (name + content axes).

    Search emits byte-identical shapes to the retired Grep/Glob tools, so one
    dispatcher covers them all:
      * ``count`` mode  -> "Found N total occurrences across M files";
      * ``files_with_matches`` header -> "Found N file(s)";
      * a "No files/matches found" sentinel;
      * ``content`` mode or a bare file listing -> count the non-empty lines.
    """
    m = _GREP_COUNT_RE.search(text)
    if m:
        return t(K.SUMMARY_GREP_MATCHES_FILES, matches=int(m.group(1)), files=int(m.group(2)))
    head = text.lstrip()
    m = _GREP_FILES_RE.match(head)
    if m:
        return t(K.SUMMARY_FOUND_FILES, count=int(m.group(1)))
    if head.startswith("No files found"):
        return t(K.SUMMARY_NO_FILES)
    if head.startswith("No matches found"):
        return t(K.SUMMARY_NO_MATCHES)
    # Either content-mode rows or a bare (header-less) file listing — both are
    # one meaningful entry per non-empty line. Drop a trailing truncation note.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and lines[-1].lstrip().startswith(_GLOB_TRUNC):
        lines = lines[:-1]
    return t(K.SUMMARY_GREP_MATCHES, count=len(lines)) if lines else t(K.SUMMARY_NO_MATCHES)


def _summary_edit(event: Any, text: str) -> Optional[str]:
    changes = getattr(event, "file_changes", None) or []
    if changes:
        added = removed = 0
        all_new = True
        for c in changes:
            old = getattr(c, "old", "") or ""
            new = getattr(c, "new", "") or ""
            a, r = _diff_counts(old, new)
            added += a
            removed += r
            if old.strip():
                all_new = False
        if all_new and added and not removed:
            return t(K.SUMMARY_CREATED_LINES, count=added)
        if added and removed:
            return t(K.SUMMARY_EDIT_ADDED_REMOVED, added=added, removed=removed)
        if added:
            return t(K.SUMMARY_EDIT_ADDED, count=added)
        if removed:
            return t(K.SUMMARY_EDIT_REMOVED, count=removed)
        return t(K.SUMMARY_UPDATED)
    m = re.search(r"All (\d+) occurrence", text)
    if m:
        return t(K.SUMMARY_REPLACED, count=int(m.group(1)))
    return None


def _result_summary(name: str, event: Any, text: str) -> Optional[str]:
    """Per-tool count summary for a *successful* tool call, or None to fall back.

    Dispatches per tool name to a small count-extractor. Bash/terminal and any
    unrecognised tool return None so the caller keeps the raw first-line summary
    (Bash output is shown verbatim, not a synthetic count).
    """
    if name == "Read":
        return _summary_read(text)
    if name == "Search":
        return _summary_search(text)
    if name == "Edit":
        return _summary_edit(event, text)
    return None


__all__ = [
    "_count_numbered_lines",
    "_diff_counts",
    "_summary_read",
    "_summary_search",
    "_summary_edit",
    "_result_summary",
]
