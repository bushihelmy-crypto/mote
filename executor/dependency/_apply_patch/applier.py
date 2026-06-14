"""Chunk applier — a Python port of the codex ``lib.rs`` apply region.

Pure text transformation: given a file's current contents and a list of parsed
:class:`UpdateFileChunk`, locate each chunk via the fuzzy
:func:`~metagpt.executor.dependency._apply_patch.seek.seek_sequence` matcher and
produce the new file contents. No filesystem IO, no Role, no permission
dependencies — the tool layer owns those.
"""
from __future__ import annotations

from typing import List, Tuple

from metagpt.executor.dependency._apply_patch.parser import (
    AddFile,
    DeleteFile,
    Hunk,
    UpdateFile,
    UpdateFileChunk,
    hunk_path,
)
from metagpt.executor.dependency._apply_patch.seek import seek_sequence

# A scheduled edit: (start index, number of old lines to drop, new lines).
_Replacement = Tuple[int, int, List[str]]


def _compute_replacements(
    original_lines: List[str], chunks: List[UpdateFileChunk]
) -> List[_Replacement]:
    """Locate each chunk in ``original_lines`` and schedule its replacement.

    Raises :class:`ApplyPatchError` (via ValueError-style message) when a chunk's
    context anchor or old lines cannot be found.
    """
    # Imported here to avoid a parser<->applier import cycle at module load.
    from metagpt.executor.dependency._apply_patch.parser import ApplyPatchError

    replacements: List[_Replacement] = []
    line_index = 0

    for chunk in chunks:
        # Narrow the search window using the chunk's context anchor, if any.
        if chunk.change_context is not None:
            idx = seek_sequence(
                original_lines, [chunk.change_context], line_index, False
            )
            if idx is None:
                raise ApplyPatchError(
                    f"Failed to find context '{chunk.change_context}'"
                )
            line_index = idx + 1

        if not chunk.old_lines:
            # Pure addition: insert at EOF (just before a trailing empty line if
            # one is present, to match standard diff line counting).
            if original_lines and original_lines[-1] == "":
                insertion_idx = len(original_lines) - 1
            else:
                insertion_idx = len(original_lines)
            replacements.append((insertion_idx, 0, list(chunk.new_lines)))
            continue

        pattern = chunk.old_lines
        new_slice = chunk.new_lines
        found = seek_sequence(original_lines, pattern, line_index, chunk.is_end_of_file)

        # The trailing empty string of a region (the file's final newline) is not
        # present in original_lines (split('\n') strips it). Retry without it.
        if found is None and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "":
                new_slice = new_slice[:-1]
            found = seek_sequence(
                original_lines, pattern, line_index, chunk.is_end_of_file
            )

        if found is None:
            raise ApplyPatchError(
                "Failed to find expected lines:\n" + "\n".join(chunk.old_lines)
            )

        replacements.append((found, len(pattern), list(new_slice)))
        line_index = found + len(pattern)

    replacements.sort(key=lambda r: r[0])
    return replacements


def _apply_replacements(
    lines: List[str], replacements: List[_Replacement]
) -> List[str]:
    """Apply replacements in descending index order so earlier edits don't shift
    the positions of later ones."""
    out = list(lines)
    for start_idx, old_len, new_segment in reversed(replacements):
        for _ in range(old_len):
            if start_idx < len(out):
                out.pop(start_idx)
        for offset, new_line in enumerate(new_segment):
            out.insert(start_idx + offset, new_line)
    return out


def apply_update(original_text: str, chunks: List[UpdateFileChunk]) -> str:
    """Return the new file contents after applying ``chunks`` to ``original_text``.

    Pure: takes and returns text (LF-normalised). Raises ``ApplyPatchError`` if a
    chunk cannot be located.
    """
    original_lines = original_text.split("\n")
    # Drop the trailing empty element from the final newline so line counts match
    # standard diff behaviour.
    if original_lines and original_lines[-1] == "":
        original_lines.pop()

    replacements = _compute_replacements(original_lines, chunks)
    new_lines = _apply_replacements(original_lines, replacements)

    # Re-append the trailing newline.
    if not (new_lines and new_lines[-1] == ""):
        new_lines.append("")
    return "\n".join(new_lines)


def affected_paths(hunks: List[Hunk]) -> List[Tuple[str, str]]:
    """Return ``(path, kind)`` for every hunk, where kind is one of
    ``add`` / ``delete`` / ``update`` / ``move``.

    The path is the patch's spelling of the destination (the move target for
    renames), used for permission matching and the read-before-write guard.
    """
    out: List[Tuple[str, str]] = []
    for hunk in hunks:
        if isinstance(hunk, AddFile):
            out.append((hunk.path, "add"))
        elif isinstance(hunk, DeleteFile):
            out.append((hunk.path, "delete"))
        elif isinstance(hunk, UpdateFile):
            kind = "move" if hunk.move_path is not None else "update"
            out.append((hunk_path(hunk), kind))
    return out
