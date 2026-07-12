"""Resolve an agent comment back onto concrete new-side line numbers.

The agent reports each finding with an ``existing_code`` snippet (the lines it
is commenting on) rather than a line number, because LLM-reported line numbers
drift. This module deterministically matches that snippet against the new-side
lines of the file's hunks and returns the ``(start_line, end_line)`` it spans.

Ported from OCR's ``ResolveComment``: normalize each side (strip the diff
prefix, trim trailing whitespace) and look for a *contiguous* run of new-side
lines equal to the snippet's lines.

When exact matching fails (the agent lightly reworded the snippet, or whitespace
drifted), a deterministic **fuzzy fallback** runs — ``difflib`` similarity above
a conservative threshold, no LLM. This is the local RE_LOCATION step: it
recovers a line number for a near-miss snippet instead of dropping it to ``L?``.
Returns ``None`` only when even the fuzzy match is too weak to trust.
"""
from __future__ import annotations

import difflib
from typing import List, Optional, Tuple

from .parser import FileDiff, HunkLine

# Fuzzy-match acceptance thresholds (difflib ratio, 0..1). Conservative so a
# genuinely unrelated snippet still resolves to None rather than mislocating.
_FUZZY_SINGLE_THRESHOLD = 0.78
_FUZZY_MULTI_THRESHOLD = 0.82


def _strip_diff_prefix(text: str) -> str:
    """Drop a leading unified-diff marker (``+``/``-``/space) from a hunk line."""
    if text and text[0] in "+- ":
        return text[1:]
    return text


def _normalize(line: str) -> str:
    """Normalize a code line for comparison (strip diff prefix + trailing ws)."""
    return _strip_diff_prefix(line).rstrip()


def _normalize_snippet(existing_code: str) -> List[str]:
    """Split *existing_code* into normalized, non-empty-trimmed lines.

    Leading/trailing fully blank lines are dropped so a snippet pasted with
    surrounding blank lines still matches. Interior blank lines are kept (they
    must line up with the source).
    """
    raw = existing_code.splitlines()
    norm = [line.rstrip() for line in raw]
    # Trim leading/trailing blank lines.
    start = 0
    end = len(norm)
    while start < end and not norm[start].strip():
        start += 1
    while end > start and not norm[end - 1].strip():
        end -= 1
    return norm[start:end]


def resolve_comment(
    existing_code: str,
    file_diff: FileDiff,
) -> Optional[Tuple[int, int]]:
    """Locate *existing_code* within *file_diff*'s new-side lines.

    Args:
        existing_code: The code snippet the agent is commenting on.
        file_diff: The parsed diff whose hunks supply the new-side lines.

    Returns:
        ``(start_line, end_line)`` (1-based, inclusive) of the matched run, or
        ``None`` when the snippet doesn't match any contiguous run.
    """
    snippet = _normalize_snippet(existing_code)
    if not snippet:
        return None

    new_lines: List[HunkLine] = file_diff.new_lines
    if not new_lines:
        return None

    norm_source = [(lineno, _normalize(text)) for lineno, text in new_lines]
    n = len(snippet)

    # Single-line snippet: match the trimmed content anywhere it appears.
    if n == 1:
        target = snippet[0].strip()
        for lineno, text in norm_source:
            if text.strip() == target and target:
                return (lineno, lineno)
        return _fuzzy_single(target, norm_source)

    # Multi-line: find a contiguous run equal to the snippet.
    for i in range(0, len(norm_source) - n + 1):
        window = norm_source[i : i + n]
        if all(window[j][1] == snippet[j] for j in range(n)):
            return (window[0][0], window[-1][0])
    return _fuzzy_multi(snippet, norm_source)


# ---------------------------------------------------------------------------
# Fuzzy fallback (local RE_LOCATION) — deterministic, no LLM.
# ---------------------------------------------------------------------------


def _ratio(a: str, b: str) -> float:
    """Similarity ratio of two strings in 0..1 (whitespace-insensitive)."""
    return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()


def _fuzzy_single(
    target: str,
    norm_source: List[Tuple[int, str]],
) -> Optional[Tuple[int, int]]:
    """Best single-line fuzzy match for *target*, if above threshold."""
    if not target:
        return None
    best_lineno: Optional[int] = None
    best_ratio = 0.0
    for lineno, text in norm_source:
        r = _ratio(target, text)
        if r > best_ratio:
            best_ratio, best_lineno = r, lineno
    if best_lineno is not None and best_ratio >= _FUZZY_SINGLE_THRESHOLD:
        return (best_lineno, best_lineno)
    return None


def _fuzzy_multi(
    snippet: List[str],
    norm_source: List[Tuple[int, str]],
) -> Optional[Tuple[int, int]]:
    """Best fuzzy match for a multi-line *snippet*.

    Slides a window of the snippet's length over the source and scores each
    window by the average per-line ratio (block boundaries aligned). Accepts the
    best window above :data:`_FUZZY_MULTI_THRESHOLD`, else ``None``.

    Deliberately does **not** anchor on a single snippet line: a multi-line
    snippet whose lines are scattered (non-contiguous) is treated as a miss, not
    relocated onto one of its lines — that would be a confident mislocation,
    exactly what the conservative thresholds exist to avoid.
    """
    n = len(snippet)
    if len(norm_source) < n:
        return None
    best_span: Optional[Tuple[int, int]] = None
    best_ratio = 0.0
    for i in range(0, len(norm_source) - n + 1):
        window = norm_source[i : i + n]
        avg = sum(_ratio(snippet[j], window[j][1]) for j in range(n)) / n
        if avg > best_ratio:
            best_ratio = avg
            best_span = (window[0][0], window[-1][0])
    if best_span is not None and best_ratio >= _FUZZY_MULTI_THRESHOLD:
        return best_span
    return None
