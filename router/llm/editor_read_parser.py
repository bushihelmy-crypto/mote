#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Parser helpers for Editor.read tool output messages.
"""
from __future__ import annotations

import re


def _strip_matching_outer_quotes(text: str) -> str:
    # Limitation: does not handle escaped quotes inside the content (e.g. "it\'s"),
    # which would be mis-stripped. Acceptable for current Editor.read output, which emits
    # raw string literals without shell-style escaping.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


EDITOR_READ_MARKER = "Command Editor.read executed:"
# Two shapes coexist in agent transcripts: older messages use `file_path=/block_content=`,
# newer messages use `path=/content=`. Keep both to parse historical logs correctly.
# The path capture uses non-greedy `.+?`, which expands via backtracking until the
# trailing `\s+(block_)?content=` matches — so paths containing spaces (e.g.
# `/Users/foo/My Drive/x.py`) are absorbed correctly, whether quoted or bare.
_SINGLE_SEGMENT_PATTERNS = [
    re.compile(
        r"^Command Editor\.read executed:\s*file_path=(?P<path>.+?)\s+block_content=(?P<content>.*)$", re.DOTALL
    ),
    re.compile(r"^Command Editor\.read executed:\s*path=(?P<path>.+?)\s+content=(?P<content>.*)$", re.DOTALL),
]

# A tool-output segment in role `_act` ends at the next segment header:
# `\n\nCommand <name> executed` (next tool output) or `\n\n\n[IMPORTANT]` (skip
# message — starts with `\n[IMPORTANT]` after the join). Requiring ` executed`
# avoids absorbing "Command ..." strings that appear inside file content.
_SEGMENT_BOUNDARY_RE = re.compile(r"\n\n(?=Command \S+ executed|\n?\[IMPORTANT\])")


def _parse_editor_read_segment(text: str) -> dict | None:
    segment = text.strip()
    for pattern in _SINGLE_SEGMENT_PATTERNS:
        match = pattern.match(segment)
        if match:
            path = match.group("path").strip().strip("'\"")
            block_content = _strip_matching_outer_quotes(match.group("content").strip())
            return {
                "file_path": path,
                "block_content": block_content,
            }
    return None


def find_editor_read_segments(text: str) -> list[dict]:
    """Locate each ``Command Editor.read executed:`` segment in ``text``.

    Each segment ends at the next tool-output boundary (nearest
    ``\\n\\nCommand <name> executed`` / ``\\n\\n[IMPORTANT]`` after the
    header), so interleaved outputs from other commands are not absorbed
    into ``block_content``.

    Returns dicts with ``file_path``, ``block_content``, ``start``, ``end``.
    ``start``/``end`` index into ``text`` and cover the full Editor.read
    segment (header through end-of-content), useful for slicing the segment
    out while leaving surrounding tool outputs intact.
    """
    results: list[dict] = []
    cursor = text.find(EDITOR_READ_MARKER)
    while cursor != -1:
        search_from = cursor + len(EDITOR_READ_MARKER)
        boundary = _SEGMENT_BOUNDARY_RE.search(text, search_from)
        end = boundary.start() if boundary else len(text)

        parsed = _parse_editor_read_segment(text[cursor:end])
        if parsed is not None:
            results.append({**parsed, "start": cursor, "end": end})

        cursor = text.find(EDITOR_READ_MARKER, end)
    return results
