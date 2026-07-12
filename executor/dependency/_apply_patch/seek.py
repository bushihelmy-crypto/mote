"""Fuzzy line-sequence matcher — a Python port of codex ``seek_sequence.rs``.

Locates a contiguous block of ``pattern`` lines inside ``lines``, starting at or
after ``start``. Matching is attempted with progressively looser strictness so a
diff authored against a slightly different copy of a file still applies:

    1. exact equality
    2. ignore trailing whitespace (rstrip)
    3. ignore leading + trailing whitespace (strip)
    4. normalise common Unicode punctuation (dashes / quotes / exotic spaces) to
       ASCII, then strip — mirrors ``git apply``'s tolerance for typographic
       drift.

When ``eof`` is true the search is biased to the tail of the file so that a
pattern intended to match the file's ending is applied at the end.

This is the line-level analogue of the character-level forgiving match in
``executor/tools/edit.py``; it is re-derived here rather than imported so the
Edit tool stays untouched (additive-only port).
"""
from __future__ import annotations

from typing import List, Optional

# Unicode → ASCII normalisation table for the final, most permissive pass.
# Mirrors the match arms in codex's ``normalise``.
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_SINGLE_QUOTES = "\u2018\u2019\u201a\u201b"
_DOUBLE_QUOTES = "\u201c\u201d\u201e\u201f"
_SPACES = (
    "\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u202f\u205f\u3000"
)

_NORMALISE_TABLE = str.maketrans(
    {
        **{c: "-" for c in _DASHES},
        **{c: "'" for c in _SINGLE_QUOTES},
        **{c: '"' for c in _DOUBLE_QUOTES},
        **{c: " " for c in _SPACES},
    }
)


def _normalise(s: str) -> str:
    """Strip then map typographic Unicode punctuation to ASCII equivalents."""
    return s.strip().translate(_NORMALISE_TABLE)


def seek_sequence(
    lines: List[str],
    pattern: List[str],
    start: int,
    eof: bool,
) -> Optional[int]:
    """Return the start index of ``pattern`` within ``lines`` at/after ``start``.

    Returns ``None`` when no match is found under any pass. An empty ``pattern``
    is a no-op match returning ``start``; a pattern longer than the input can
    never match and returns ``None`` (guards against out-of-bounds slicing).
    """
    if not pattern:
        return start

    if len(pattern) > len(lines):
        return None

    if eof and len(lines) >= len(pattern):
        search_start = len(lines) - len(pattern)
    else:
        search_start = start

    last = len(lines) - len(pattern)

    # Pass 1: exact match.
    for i in range(search_start, last + 1):
        if lines[i : i + len(pattern)] == pattern:
            return i

    # Pass 2: ignore trailing whitespace.
    for i in range(search_start, last + 1):
        if all(lines[i + j].rstrip() == pat.rstrip() for j, pat in enumerate(pattern)):
            return i

    # Pass 3: ignore leading + trailing whitespace.
    for i in range(search_start, last + 1):
        if all(lines[i + j].strip() == pat.strip() for j, pat in enumerate(pattern)):
            return i

    # Pass 4: normalise typographic Unicode punctuation, then compare.
    for i in range(search_start, last + 1):
        if all(
            _normalise(lines[i + j]) == _normalise(pat) for j, pat in enumerate(pattern)
        ):
            return i

    return None
