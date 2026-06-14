"""Patch grammar parser — a Python port of codex ``parser.rs`` + ``streaming_parser.rs``.

Parses the codex ``apply_patch`` freeform text into a list of structured hunks
(Add / Delete / Update, with Update carrying an optional Move destination and a
list of context-anchored chunks). It validates structure only — it does not
touch the filesystem (that is the tool's job, via :mod:`applier`).

The format (the "grammar" the model must emit)::

    *** Begin Patch
    *** Add File: path/to/new.py
    +line one
    +line two
    *** Delete File: path/to/gone.py
    *** Update File: path/to/edit.py
    *** Move to: path/to/renamed.py        (optional, Update only)
    @@ optional context anchor
     unchanged context line
    -removed line
    +added line
    *** End of File                        (optional, marks chunk at EOF)
    *** End Patch

Inside an Update hunk each line begins with one of: a leading space (context),
``+`` (added), ``-`` (removed), or ``@@`` (a new chunk, optionally with a
single-line context anchor after it). ``*** End of File`` marks the current
chunk as anchored to the end of the file.

A lenient mode strips a surrounding ``<<EOF`` / ``<<'EOF'`` / ``<<"EOF"`` heredoc
wrapper (GPT-4.1 sometimes formats the call that way) before parsing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union

# --- Markers (byte-for-byte the codex constants) ---
BEGIN_PATCH_MARKER = "*** Begin Patch"
END_PATCH_MARKER = "*** End Patch"
ADD_FILE_MARKER = "*** Add File: "
DELETE_FILE_MARKER = "*** Delete File: "
UPDATE_FILE_MARKER = "*** Update File: "
MOVE_TO_MARKER = "*** Move to: "
EOF_MARKER = "*** End of File"
CHANGE_CONTEXT_MARKER = "@@ "
EMPTY_CHANGE_CONTEXT_MARKER = "@@"

_INVALID_HUNK_HEADER = (
    "'{got}' is not a valid hunk header. Valid hunk headers: "
    "'*** Add File: {{path}}', '*** Delete File: {{path}}', "
    "'*** Update File: {{path}}'"
)
_UNEXPECTED_UPDATE_LINE = (
    "Unexpected line found in update hunk: '{line}'. Every line should start "
    "with ' ' (context line), '+' (added line), or '-' (removed line)"
)


class ApplyPatchError(Exception):
    """A patch could not be parsed (or, from the applier, could not be applied).

    Carries the optional 1-based ``line_no`` where parsing failed so the tool can
    surface codex-style explicit diagnostics.
    """

    def __init__(self, message: str, line_no: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.line_no = line_no

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.line_no is not None:
            return f"invalid hunk at line {self.line_no}, {self.message}"
        return self.message


# ---------------------------------------------------------------------------
# Hunk dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AddFile:
    path: str
    contents: str = ""


@dataclass
class DeleteFile:
    path: str


@dataclass
class UpdateFileChunk:
    change_context: Optional[str] = None
    old_lines: List[str] = field(default_factory=list)
    new_lines: List[str] = field(default_factory=list)
    is_end_of_file: bool = False


@dataclass
class UpdateFile:
    path: str
    move_path: Optional[str] = None
    chunks: List[UpdateFileChunk] = field(default_factory=list)


Hunk = Union[AddFile, DeleteFile, UpdateFile]


def hunk_path(hunk: Hunk) -> str:
    """The path this hunk affects (the move destination for renames)."""
    if isinstance(hunk, UpdateFile) and hunk.move_path is not None:
        return hunk.move_path
    return hunk.path


# ---------------------------------------------------------------------------
# Streaming state machine (port of streaming_parser.rs)
# ---------------------------------------------------------------------------

# Parser modes.
_NOT_STARTED = "NotStarted"
_STARTED_PATCH = "StartedPatch"
_ADD_FILE = "AddFile"
_DELETE_FILE = "DeleteFile"
_UPDATE_FILE = "UpdateFile"
_ENDED_PATCH = "EndedPatch"


class _StreamingPatchParser:
    """Line-by-line state machine that accumulates parsed hunks."""

    def __init__(self) -> None:
        self._line_buffer = ""
        self._mode = _NOT_STARTED
        self._hunks: List[Hunk] = []
        self._line_number = 0
        # The line number where the current Update hunk header appeared.
        self._update_hunk_line_number = 0

    # -- public driving API --

    def push_delta(self, delta: str) -> None:
        for ch in delta:
            if ch == "\n":
                line = self._line_buffer
                self._line_buffer = ""
                if line.endswith("\r"):
                    line = line[:-1]
                self._line_number += 1
                self._process_line(line)
            else:
                self._line_buffer += ch

    def finish(self) -> List[Hunk]:
        if self._line_buffer:
            line = self._line_buffer
            self._line_buffer = ""
            self._line_number += 1
            if line.strip() == END_PATCH_MARKER:
                self._ensure_update_hunk_is_not_empty(line.strip())
                self._mode = _ENDED_PATCH
            else:
                self._process_line(line)

        if self._mode != _ENDED_PATCH:
            raise ApplyPatchError(
                "The last line of the patch must be '*** End Patch'"
            )
        return self._hunks

    # -- helpers --

    def _ensure_update_hunk_is_not_empty(self, line: str) -> None:
        if not self._hunks:
            return
        last = self._hunks[-1]
        if not isinstance(last, UpdateFile):
            return
        if not last.chunks and self._mode == _UPDATE_FILE:
            raise ApplyPatchError(
                f"Update file hunk for path '{last.path}' is empty",
                self._update_hunk_line_number,
            )
        if last.chunks:
            chunk = last.chunks[-1]
            if not chunk.old_lines and not chunk.new_lines:
                if line == END_PATCH_MARKER:
                    raise ApplyPatchError(
                        "Update hunk does not contain any lines", self._line_number
                    )
                raise ApplyPatchError(
                    _UNEXPECTED_UPDATE_LINE.format(line=line), self._line_number
                )

    def _handle_hunk_headers_and_end_patch(self, trimmed: str) -> bool:
        if trimmed == END_PATCH_MARKER:
            self._ensure_update_hunk_is_not_empty(trimmed)
            self._mode = _ENDED_PATCH
            return True
        if trimmed.startswith(ADD_FILE_MARKER):
            self._ensure_update_hunk_is_not_empty(trimmed)
            self._hunks.append(AddFile(path=trimmed[len(ADD_FILE_MARKER):], contents=""))
            self._mode = _ADD_FILE
            return True
        if trimmed.startswith(DELETE_FILE_MARKER):
            self._ensure_update_hunk_is_not_empty(trimmed)
            self._hunks.append(DeleteFile(path=trimmed[len(DELETE_FILE_MARKER):]))
            self._mode = _DELETE_FILE
            return True
        if trimmed.startswith(UPDATE_FILE_MARKER):
            self._ensure_update_hunk_is_not_empty(trimmed)
            self._hunks.append(UpdateFile(path=trimmed[len(UPDATE_FILE_MARKER):]))
            self._mode = _UPDATE_FILE
            self._update_hunk_line_number = self._line_number
            return True
        return False

    def _process_line(self, line: str) -> None:
        trimmed = line.strip()
        if self._mode == _NOT_STARTED:
            if trimmed == BEGIN_PATCH_MARKER:
                self._mode = _STARTED_PATCH
                return
            raise ApplyPatchError(
                "The first line of the patch must be '*** Begin Patch'"
            )

        if self._mode == _STARTED_PATCH:
            if self._handle_hunk_headers_and_end_patch(trimmed):
                return
            raise ApplyPatchError(
                _INVALID_HUNK_HEADER.format(got=trimmed), self._line_number
            )

        if self._mode == _ADD_FILE:
            if self._handle_hunk_headers_and_end_patch(trimmed):
                return
            if line.startswith("+"):
                last = self._hunks[-1]
                if isinstance(last, AddFile):
                    last.contents += line[1:] + "\n"
                    return
            raise ApplyPatchError(
                _INVALID_HUNK_HEADER.format(got=trimmed), self._line_number
            )

        if self._mode == _DELETE_FILE:
            if self._handle_hunk_headers_and_end_patch(trimmed):
                return
            raise ApplyPatchError(
                _INVALID_HUNK_HEADER.format(got=trimmed), self._line_number
            )

        if self._mode == _UPDATE_FILE:
            self._process_update_line(line)
            return

        # _ENDED_PATCH
        if trimmed == "":
            return
        raise ApplyPatchError(
            "The last line of the patch must be '*** End Patch'"
        )

    def _process_update_line(self, line: str) -> None:
        update_line = line.rstrip()
        if self._handle_hunk_headers_and_end_patch(update_line):
            return

        last = self._hunks[-1]
        assert isinstance(last, UpdateFile)
        chunks = last.chunks

        def last_chunk_empty() -> bool:
            return bool(chunks) and not chunks[-1].old_lines and not chunks[-1].new_lines

        # After an end-of-file chunk, only a @@ context marker (or blank) may follow.
        if chunks and chunks[-1].is_end_of_file:
            if update_line == "":
                return
            if update_line != EMPTY_CHANGE_CONTEXT_MARKER and not update_line.startswith(
                CHANGE_CONTEXT_MARKER
            ):
                raise ApplyPatchError(
                    f"Expected update hunk to start with a @@ context marker, got: '{line}'",
                    self._line_number,
                )

        # Move destination (only valid before any chunk and only once).
        if not chunks and last.move_path is None and update_line.startswith(MOVE_TO_MARKER):
            last.move_path = update_line[len(MOVE_TO_MARKER):]
            return

        # A @@ marker immediately after an empty chunk is invalid.
        if (
            update_line == EMPTY_CHANGE_CONTEXT_MARKER
            or update_line.startswith(CHANGE_CONTEXT_MARKER)
        ) and last_chunk_empty():
            raise ApplyPatchError(
                _UNEXPECTED_UPDATE_LINE.format(line=line), self._line_number
            )

        if update_line == EMPTY_CHANGE_CONTEXT_MARKER:
            chunks.append(UpdateFileChunk(change_context=None))
            return

        if update_line.startswith(CHANGE_CONTEXT_MARKER):
            chunks.append(
                UpdateFileChunk(change_context=update_line[len(CHANGE_CONTEXT_MARKER):])
            )
            return

        if update_line == EOF_MARKER:
            if last_chunk_empty():
                raise ApplyPatchError(
                    "Update hunk does not contain any lines", self._line_number
                )
            if chunks:
                chunks[-1].is_end_of_file = True
            return

        # A bare empty line is treated as an empty context line.
        if line == "":
            if not chunks:
                chunks.append(UpdateFileChunk())
            chunks[-1].old_lines.append("")
            chunks[-1].new_lines.append("")
            return

        if line.startswith(" "):
            if not chunks:
                chunks.append(UpdateFileChunk())
            chunks[-1].old_lines.append(line[1:])
            chunks[-1].new_lines.append(line[1:])
            return

        if line.startswith("+"):
            if not chunks:
                chunks.append(UpdateFileChunk())
            chunks[-1].new_lines.append(line[1:])
            return

        if line.startswith("-"):
            if not chunks:
                chunks.append(UpdateFileChunk())
            chunks[-1].old_lines.append(line[1:])
            return

        if chunks and (chunks[-1].old_lines or chunks[-1].new_lines):
            raise ApplyPatchError(
                f"Expected update hunk to start with a @@ context marker, got: '{line}'",
                self._line_number,
            )

        raise ApplyPatchError(
            _UNEXPECTED_UPDATE_LINE.format(line=line), self._line_number
        )


# ---------------------------------------------------------------------------
# Boundary checks + public entry point (port of parser.rs)
# ---------------------------------------------------------------------------


def _check_start_and_end_lines_strict(lines: List[str]) -> None:
    first = lines[0].strip() if lines else None
    last = lines[-1].strip() if lines else None
    if first == BEGIN_PATCH_MARKER and last == END_PATCH_MARKER:
        return
    if first != BEGIN_PATCH_MARKER:
        raise ApplyPatchError("The first line of the patch must be '*** Begin Patch'")
    raise ApplyPatchError("The last line of the patch must be '*** End Patch'")


def _check_patch_boundaries_lenient(lines: List[str]) -> List[str]:
    """Strip a surrounding heredoc wrapper if present, else require strict bounds."""
    try:
        _check_start_and_end_lines_strict(lines)
        return lines
    except ApplyPatchError as strict_error:
        if (
            len(lines) >= 4
            and lines[0] in ("<<EOF", "<<'EOF'", '<<"EOF"')
            and lines[-1].endswith("EOF")
        ):
            inner = lines[1:-1]
            _check_start_and_end_lines_strict(inner)
            return inner
        raise strict_error


def parse_patch(patch: str) -> List[Hunk]:
    """Parse codex ``apply_patch`` text into a list of hunks.

    Lenient by default: tolerates leading/trailing whitespace around markers and
    a surrounding heredoc wrapper. Raises :class:`ApplyPatchError` on malformed
    input.
    """
    lines = patch.strip().split("\n") if patch.strip() else []
    patch_lines = _check_patch_boundaries_lenient(lines)

    parser = _StreamingPatchParser()
    parser.push_delta("\n".join(patch_lines))
    return parser.finish()
