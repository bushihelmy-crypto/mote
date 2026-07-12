"""Unified-diff parser for the code-review pipeline.

Parses ``git diff`` / ``git show`` output into a list of :class:`FileDiff`.
Only the *new-side* lines of each hunk are retained (context + added lines)
paired with their new-side line numbers, which is what the comment resolver
needs to locate a comment back onto concrete lines.

Pure stdlib — no external diff library. The grammar handled is the standard
unified-diff surface git emits:

    diff --git a/path b/path
    <optional mode / index / similarity lines>
    --- a/old           (or ``--- /dev/null`` for a new file)
    +++ b/new           (or ``+++ /dev/null`` for a deleted file)
    @@ -l,s +l,s @@ optional section heading
     context line
    -removed line
    +added line
    \\ No newline at end of file

Binary files surface as ``Binary files a/x and b/y differ`` with no hunks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# new-side line: (new_lineno, text) for every context + added line in a hunk.
HunkLine = Tuple[int, str]

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    """One ``@@ ... @@`` hunk, keeping only the new-side lines."""

    new_start: int
    # (new_lineno, text) for each context/added line (removed lines excluded).
    lines: List[HunkLine] = field(default_factory=list)


@dataclass
class FileDiff:
    """Parsed diff for a single file.

    ``path`` is the new-side path (post-rename / post-create). ``hunks`` carry
    the new-side lines used for comment resolution; binary/deleted files have
    none.
    """

    path: str
    old_path: Optional[str] = None
    is_binary: bool = False
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    hunks: List[Hunk] = field(default_factory=list)
    # Deterministically-derived sibling paths worth reading for context (filled
    # by the filter/bundling step, not the parser). Surfaced to the reviewer
    # agent's prompt; empty by default.
    related: List[str] = field(default_factory=list)

    @property
    def new_lines(self) -> List[HunkLine]:
        """Flattened new-side lines across all hunks (in file order)."""
        out: List[HunkLine] = []
        for h in self.hunks:
            out.extend(h.lines)
        return out

    def added_count(self) -> int:
        """Number of added lines (rough change magnitude)."""
        return sum(1 for h in self.hunks for _, text in h.lines if text.startswith("+"))


def _strip_path_prefix(path: str) -> str:
    """Drop a leading ``a/`` or ``b/`` git path prefix."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_unified_diff(text: str) -> List[FileDiff]:
    """Parse unified-diff *text* into a list of :class:`FileDiff`.

    Robust to the optional metadata lines git emits (``index``, ``old mode``,
    ``similarity index``, ``rename from/to``, ``new file mode`` …). Lines that
    don't belong to any file section are ignored.
    """
    files: List[FileDiff] = []
    current: Optional[FileDiff] = None
    current_hunk: Optional[Hunk] = None
    new_lineno = 0

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        m = _DIFF_GIT_RE.match(line)
        if m:
            # Start a new file section.
            old_p, new_p = _strip_path_prefix(m.group(1)), _strip_path_prefix(m.group(2))
            current = FileDiff(path=new_p, old_path=old_p)
            current_hunk = None
            files.append(current)
            i += 1
            continue

        if current is None:
            i += 1
            continue

        if line.startswith("new file mode"):
            current.is_new = True
        elif line.startswith("deleted file mode"):
            current.is_deleted = True
        elif line.startswith("rename from") or line.startswith("rename to"):
            current.is_rename = True
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.is_binary = True
        elif line.startswith("--- "):
            old = line[4:].strip()
            if old == "/dev/null":
                current.is_new = True
            elif old.startswith(("a/", "b/")):
                current.old_path = _strip_path_prefix(old)
        elif line.startswith("+++ "):
            new = line[4:].strip()
            if new == "/dev/null":
                current.is_deleted = True
            elif new.startswith(("a/", "b/")):
                current.path = _strip_path_prefix(new)
        else:
            hm = _HUNK_RE.match(line)
            if hm:
                new_start = int(hm.group(1))
                current_hunk = Hunk(new_start=new_start)
                current.hunks.append(current_hunk)
                new_lineno = new_start
                i += 1
                continue
            if current_hunk is not None:
                if line.startswith("\\"):
                    # "\ No newline at end of file" — metadata, not a content line.
                    i += 1
                    continue
                if line.startswith("+"):
                    current_hunk.lines.append((new_lineno, line))
                    new_lineno += 1
                elif line.startswith("-"):
                    # Removed line — not on the new side; line number unchanged.
                    pass
                elif line.startswith(" ") or line == "":
                    # Context line (a fully blank context line shows as "").
                    current_hunk.lines.append((new_lineno, line))
                    new_lineno += 1
                # Any other prefix ends the hunk's relevance; ignore.
        i += 1

    return files
