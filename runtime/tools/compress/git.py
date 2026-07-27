"""Compressor for ``git status`` / ``git log`` / ``git diff`` output.

git's porcelain-adjacent text output is verbose and repetitive: status hint
lines, full commit bodies, and diff hunks dominate the token count while the
signal (which files changed, which commits, +/- magnitude) is small. Each
sub-command gets a targeted strategy that keeps the signal and drops the bulk.
"""

from __future__ import annotations

import re

from mote.contracts.text import Elision, ElisionStrategy, ElisionUnit
from mote.runtime.tools.compress.base import CompressionResult, applied, unchanged

# A ``git log --oneline`` row: ``<sha> <subject>`` — already compact, kept as-is.
_ONELINE_RE = re.compile(r"^[0-9a-f]{7,40}\b")

# Metadata / structural lines in a unified diff that are always worth keeping.
_DIFF_KEEP_PREFIXES = (
    "index ",
    "--- ",
    "+++ ",
    "@@",
    "similarity index",
    "dissimilarity index",
    "rename from",
    "rename to",
    "copy from",
    "copy to",
    "new file",
    "deleted file",
    "old mode",
    "new mode",
    "Binary files",
    "GIT binary patch",
)

# Commit-header lines in ``git log`` default format.
_LOG_HEADER_PREFIXES = (
    "commit ",
    "Author:",
    "AuthorDate:",
    "Commit:",
    "CommitDate:",
    "Date:",
    "Merge:",
)

# How many entries to keep per ``git status`` section before summarising.
_STATUS_KEEP = 10
# Per-file changed-line budget in ``git diff`` before summarising the rest.
_DIFF_BODY_BUDGET = 12


class GitCompressor:
    """Structure-aware compressor for the three verbose git read commands."""

    prefixes: tuple[str, ...] = ("git status", "git log", "git diff")

    def compress(self, output: str, *, argv: list[str]) -> CompressionResult:
        sub = argv[1] if len(argv) > 1 else ""
        if sub == "status":
            return self._result(output, self._compress_status(output), "git status")
        if sub == "log":
            return self._result(output, self._compress_log(output), "git log")
        if sub == "diff":
            return self._result(output, self._compress_diff(output), "git diff")
        # Any other git sub-command: leave it alone (small output anyway).
        return unchanged(output, label="git")

    @staticmethod
    def _result(original: str, text: str, label: str) -> CompressionResult:
        return applied(original, text, label)

    # ------------------------------------------------------------------

    @staticmethod
    def _compress_status(output: str) -> str:
        """Keep headers + branch lines; cap entry lists; drop hints/blanks."""
        out: list[str] = []
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            out.extend(buffer[:_STATUS_KEEP])
            if len(buffer) > _STATUS_KEEP:
                out.append(f"  ... and {len(buffer) - _STATUS_KEEP} more")
            buffer.clear()

        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("(use"):  # drop the "(use git ...)" hints
                continue
            indented = line[:1] in (" ", "\t")
            if indented and stripped:
                buffer.append(line)
            else:
                flush()
                if stripped:  # drop blank separators
                    out.append(line)
        flush()
        return "\n".join(out)

    @staticmethod
    def _compress_log(output: str) -> str:
        """Keep commit/author/date headers + subject; drop the message body."""
        out: list[str] = []
        subject_taken = False
        for line in output.splitlines():
            if line.startswith("commit "):
                out.append(line)
                subject_taken = False
                continue
            if line.startswith(_LOG_HEADER_PREFIXES):
                out.append(line)
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if _ONELINE_RE.match(line) and not line.startswith(" "):
                out.append(line)  # --oneline row, keep verbatim
                continue
            if not subject_taken:
                out.append("    " + stripped)  # first body line == subject
                subject_taken = True
            # subsequent body lines: dropped
        return "\n".join(out)

    @staticmethod
    def _compress_diff(output: str) -> str:
        """Per file: keep structural lines + hunk headers; budget the body."""
        out: list[str] = []
        added = removed = dropped = 0
        budget = _DIFF_BODY_BUDGET

        def flush() -> None:
            nonlocal added, removed, dropped
            if dropped:
                el = Elision(ElisionUnit.LINES, dropped, added + removed, ElisionStrategy.TAIL)
                out.append("  " + el.render_for_model(noun="more changed lines", extra=f"(+{added} -{removed} total)"))
            added = removed = dropped = 0

        for line in output.splitlines():
            if line.startswith("diff --git"):
                flush()
                budget = _DIFF_BODY_BUDGET
                out.append(line)
            elif line.startswith(_DIFF_KEEP_PREFIXES):
                out.append(line)
            elif line.startswith("+"):
                added += 1
                if budget > 0:
                    out.append(line)
                    budget -= 1
                else:
                    dropped += 1
            elif line.startswith("-"):
                removed += 1
                if budget > 0:
                    out.append(line)
                    budget -= 1
                else:
                    dropped += 1
            else:  # context line
                if budget > 0:
                    out.append(line)
                    budget -= 1
                else:
                    dropped += 1
        flush()
        return "\n".join(out)
