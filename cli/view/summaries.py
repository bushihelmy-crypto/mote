#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Per-tool result summaries (claude-code's "Read N lines" / "Found N files" …).

The projector computes a count-based one-liner ONCE per tool (窄腰) so a human
reads a meaningful chrome line ("读取 42 行") instead of the raw first line of
output (e.g. Read's ``     1→import os``). Chinese wording per the CLI's
convention. :func:`_result_summary` returns ``None`` for tools/shapes with no
honest count → the caller falls back to the first-non-empty-line summary
(Bash/terminal/unknown keep their raw output line, exactly like claude-code).

Pure functions only (``(name, event, text) → Optional[str]``): no I/O, no
projector state. Split out of ``projector.py`` so the fold's main class stays
focused on the ``AgentEvent → ViewEvent`` fold itself.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# A Read body line: right-justified number + ``→`` (U+2192) + content.
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\u2192")
# Grep ``count`` mode summary: "Found N total occurrences across M files".
_GREP_COUNT_RE = re.compile(r"Found (\d+) total occurrences? across (\d+) files?")
# Grep ``files_with_matches`` header: "Found N file(s)".
_GREP_FILES_RE = re.compile(r"^Found (\d+) files?")
# Write success message tail: "(N line(s), B bytes written)" after Created/Updated.
_WRITE_RE = re.compile(r"(Created|Updated) .*?\((\d+) line")
# Glob's trailing non-path note emitted at the result cap.
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
        return "读取图片"
    if head.startswith("Read PDF"):
        return "读取 PDF"
    n = _count_numbered_lines(text)
    return f"读取 {n} 行" if n else None


def _summary_grep(text: str) -> Optional[str]:
    m = _GREP_COUNT_RE.search(text)
    if m:
        return f"找到 {m.group(1)} 处匹配，共 {m.group(2)} 个文件"
    head = text.lstrip()
    m = _GREP_FILES_RE.match(head)
    if m:
        return f"找到 {m.group(1)} 个文件"
    if head.startswith(("No files found", "No matches found")):
        return "无匹配"
    # ``content`` mode has no header — count the matching body lines.
    n = sum(1 for ln in text.splitlines() if ln.strip())
    return f"找到 {n} 处匹配" if n else "无匹配"


def _summary_glob(text: str) -> Optional[str]:
    head = text.lstrip()
    if head.startswith("No files found"):
        return "无匹配文件"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and lines[-1].lstrip().startswith(_GLOB_TRUNC):
        lines = lines[:-1]
    return f"找到 {len(lines)} 个文件" if lines else "无匹配文件"


def _summary_write(text: str) -> Optional[str]:
    m = _WRITE_RE.search(text)
    if not m:
        return None
    verb = "新建" if m.group(1) == "Created" else "更新"
    return f"{verb} {m.group(2)} 行"


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
            return f"新建 {added} 行"
        if added and removed:
            return f"更新 +{added} -{removed} 行"
        if added:
            return f"更新 +{added} 行"
        if removed:
            return f"更新 -{removed} 行"
        return "已更新"
    m = re.search(r"All (\d+) occurrence", text)
    if m:
        return f"替换 {m.group(1)} 处"
    return None


def _result_summary(name: str, event: Any, text: str) -> Optional[str]:
    """CC-style count summary for a *successful* tool call, or None to fall back.

    Dispatches per tool name to a small count-extractor. Bash/terminal and any
    unrecognised tool return None so the caller keeps the raw first-line summary
    (claude-code shows Bash output verbatim, not a synthetic count).
    """
    if name == "Read":
        return _summary_read(text)
    if name == "Grep":
        return _summary_grep(text)
    if name == "Glob":
        return _summary_glob(text)
    if name == "Write":
        return _summary_write(text)
    if name == "Edit":
        return _summary_edit(event, text)
    return None


__all__ = [
    "_count_numbered_lines",
    "_diff_counts",
    "_summary_read",
    "_summary_grep",
    "_summary_glob",
    "_summary_write",
    "_summary_edit",
    "_result_summary",
]
