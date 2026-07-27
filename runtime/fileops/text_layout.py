"""One logical line model shared by Search and Read views."""

from __future__ import annotations

import bisect
import re

_NEWLINE = re.compile(r"\r\n|\r|\n")


def text_layout(text: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    starts = [0]
    lines: list[str] = []
    for newline in _NEWLINE.finditer(text):
        lines.append(text[starts[-1] : newline.start()])
        starts.append(newline.end())
    lines.append(text[starts[-1] :])
    return tuple(starts), tuple(lines)


def line_number_at(line_starts: tuple[int, ...], character_offset: int) -> int:
    return bisect.bisect_right(line_starts, character_offset)


def text_page(
    text: str,
    *,
    offset: int,
    limit: int,
) -> tuple[tuple[str, ...], int]:
    selected: list[str] = []
    line_number = 1
    line_start = 0
    selection_end = offset + limit
    for newline in _NEWLINE.finditer(text):
        if offset <= line_number < selection_end:
            selected.append(text[line_start : newline.start()])
        line_number += 1
        line_start = newline.end()
    if offset <= line_number < selection_end:
        selected.append(text[line_start:])
    return tuple(selected), line_number


__all__ = ["line_number_at", "text_layout", "text_page"]
