"""Pure hunk algebra for diff tracking.

A *hunk* is one contiguous block of change between two revisions of a text:
its line geometry (mirroring a unified-diff ``@@`` header) plus the exact old
and new line content needed to apply or revert it in isolation.

This module is a domain-agnostic leaf (stdlib :mod:`difflib` only). It knows
*nothing* about who made a change, file paths, persistence, or event delivery —
those are tracking concerns owned by the session-layer hunk ledger, which wraps
these value hunks with attribution. Keeping the algebra pure means it is
trivially testable and reusable by any subsystem that needs line-level change
attribution.

Line numbers are **1-indexed** to match unified-diff convention. A pure
insertion has ``old_count == 0``; a pure deletion has ``new_count == 0``.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

__all__ = [
    "Hunk",
    "HunkApplyError",
    "MAX_DIFF_SIZE_BYTES",
    "patch_lines",
    "slice_lines",
    "split_hunks",
    "apply_hunk",
    "revert_hunk",
    "apply_hunks",
    "revert_hunks",
]

# Files larger than this are skipped to avoid pathological diff behaviour.
MAX_DIFF_SIZE_BYTES = 1024 * 1024  # 1 MiB


class HunkApplyError(Exception):
    """Raised when a hunk cannot be applied/reverted against given content.

    Signals that the content at the hunk's target lines does not match what the
    hunk expects (i.e. the file has drifted), so applying it blindly would
    corrupt the text.
    """


@dataclass(frozen=True)
class Hunk:
    """One contiguous change between a baseline and current revision.

    A pure value type: two hunks are equal iff their geometry and content are
    equal, so hunks compare by *what changed*, never by identity.
    """

    old_start: int
    """1-indexed start line in the baseline (old) text."""
    old_count: int
    """Number of baseline lines this hunk replaces/deletes (0 for insertion)."""
    new_start: int
    """1-indexed start line in the current (new) text."""
    new_count: int
    """Number of current lines this hunk adds/replaces (0 for deletion)."""
    old_text: Optional[str]
    """The removed/changed baseline lines (with newlines), ``None`` for a pure
    insertion."""
    new_text: str
    """The added/changed current lines (with newlines), ``""`` for a pure
    deletion."""

    @property
    def is_insertion(self) -> bool:
        """True if this hunk only adds lines (removes nothing)."""
        return self.old_count == 0

    @property
    def is_deletion(self) -> bool:
        """True if this hunk only removes lines (adds nothing)."""
        return self.new_count == 0

    def header(self) -> str:
        """The unified-diff hunk header, e.g. ``@@ -2,1 +2,1 @@``."""
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"

    def summary(self) -> str:
        """A short ``+adds/-dels`` counter for display."""
        adds = len(self.new_text.splitlines()) if self.new_text else 0
        dels = len(self.old_text.splitlines()) if self.old_text else 0
        return f"+{adds}/-{dels}"

    @classmethod
    def file_created(cls, content: str) -> "Hunk":
        """A hunk representing a whole new file (no baseline)."""
        return cls(
            old_start=0,
            old_count=0,
            new_start=1,
            new_count=max(len(content.splitlines()), 1),
            old_text=None,
            new_text=content,
        )

    @classmethod
    def file_deleted(cls, content: str) -> "Hunk":
        """A hunk representing a whole deleted file."""
        return cls(
            old_start=1,
            old_count=max(len(content.splitlines()), 1),
            new_start=0,
            new_count=0,
            old_text=content,
            new_text="",
        )


def patch_lines(content: str, start_line: int, remove_count: int, insert_text: str) -> str:
    """Replace ``remove_count`` lines at ``start_line`` (1-indexed) with ``insert_text``.

    ``remove_count`` may be 0 (pure insertion) and ``insert_text`` may be empty
    (pure deletion). The trailing newline of ``content`` is preserved. Indices
    saturate at the content bounds so out-of-range values never raise.
    """
    lines = content.splitlines()
    start_idx = max(start_line - 1, 0)

    result: list[str] = list(lines[: min(start_idx, len(lines))])
    if insert_text:
        result.extend(insert_text.splitlines())
    end_idx = min(start_idx + remove_count, len(lines))
    result.extend(lines[end_idx:])

    output = "\n".join(result)
    if content.endswith("\n") and output:
        output += "\n"
    return output


def split_hunks(baseline: str, current: str) -> list[Hunk]:
    """Compute the contiguous change hunks between ``baseline`` and ``current``.

    Line-based diff: any run of equal lines closes the in-progress hunk, so each
    returned hunk is a maximal block of adjacent changed lines separated by at
    least one unchanged line. Returns ``[]`` when the texts are identical or
    either exceeds :data:`MAX_DIFF_SIZE_BYTES`.
    """
    if baseline == current:
        return []
    if len(baseline) > MAX_DIFF_SIZE_BYTES or len(current) > MAX_DIFF_SIZE_BYTES:
        return []

    old_lines = baseline.splitlines(keepends=True)
    new_lines = current.splitlines(keepends=True)

    hunks: list[Hunk] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        removed = old_lines[i1:i2]
        added = new_lines[j1:j2]
        hunks.append(
            Hunk(
                old_start=i1 + 1,
                old_count=len(removed),
                new_start=j1 + 1,
                new_count=len(added),
                old_text="".join(removed) if removed else None,
                new_text="".join(added),
            )
        )
    return hunks


def slice_lines(content: str, start_line: int, count: int) -> str:
    """Join ``count`` lines of ``content`` starting at ``start_line`` (1-indexed).

    ``start_line`` saturates at 0; a ``count`` of 0 (or on empty ``content``)
    yields ``""``. Shared by the hunk apply/verify path and the review-side
    rehydration that slices a before-image / live file at a hunk's line range.
    """
    lines = content.splitlines(keepends=True)
    start_idx = max(start_line - 1, 0)
    return "".join(lines[start_idx : start_idx + count])


def apply_hunk(baseline: str, hunk: Hunk, *, verify: bool = True) -> str:
    """Apply (accept) ``hunk`` to ``baseline``, folding the change into it.

    With ``verify`` (default), the baseline lines at the hunk's old range must
    match ``hunk.old_text`` or :class:`HunkApplyError` is raised (drift guard).
    """
    if verify:
        found = slice_lines(baseline, hunk.old_start, hunk.old_count)
        expected = hunk.old_text or ""
        if found != expected:
            raise HunkApplyError(
                f"cannot apply hunk at line {hunk.old_start}: baseline drifted "
                f"(expected {expected!r}, found {found!r})"
            )
    return patch_lines(baseline, hunk.old_start, hunk.old_count, hunk.new_text)


def revert_hunk(current: str, hunk: Hunk, *, verify: bool = True) -> str:
    """Revert (reject) ``hunk`` from ``current``, restoring the baseline lines.

    With ``verify`` (default), the current lines at the hunk's new range must
    match ``hunk.new_text`` or :class:`HunkApplyError` is raised (drift guard).
    """
    if verify:
        found = slice_lines(current, hunk.new_start, hunk.new_count)
        if found != hunk.new_text:
            raise HunkApplyError(
                f"cannot revert hunk at line {hunk.new_start}: content drifted "
                f"(expected {hunk.new_text!r}, found {found!r})"
            )
    return patch_lines(current, hunk.new_start, hunk.new_count, hunk.old_text or "")


def apply_hunks(baseline: str, hunks: list[Hunk], *, verify: bool = True) -> str:
    """Apply several hunks to one baseline, highest line first so earlier hunks'
    line numbers stay valid as later ones are patched in."""
    result = baseline
    for hunk in sorted(hunks, key=lambda h: h.old_start, reverse=True):
        result = apply_hunk(result, hunk, verify=verify)
    return result


def revert_hunks(current: str, hunks: list[Hunk], *, verify: bool = True) -> str:
    """Revert several hunks from one current text, highest line first so earlier
    hunks' line numbers stay valid as later ones are reverted."""
    result = current
    for hunk in sorted(hunks, key=lambda h: h.new_start, reverse=True):
        result = revert_hunk(result, hunk, verify=verify)
    return result
