"""Write file tool.

Writes ``content`` to a file on the local filesystem, creating the file (and any
missing parent directories) or overwriting it if it already exists. The result
reports whether the file was created or updated and how many lines/bytes were
written.

Differences by design:
- Read-before-overwrite is enforced via the Role's shared file-read state
  (Role.get_file_read_mtime): an existing
  file must have been read this session, and must not have changed on disk since
  that read, before it can be overwritten. When the tool is used unbound (no
  Role injected the capability), the check is skipped so it still works in
  isolation/tests.
- When overwriting, the existing file's newline style (LF vs CRLF) and text
  encoding are detected and preserved, round-tripping line endings,
  so writes don't silently rewrite every line of a CRLF file.
"""
from __future__ import annotations

import os
from typing import ClassVar

from mote.common.const.tools import MAX_CONTENT_SIZE_BYTES
from mote.common.text import count_noun
from mote.executor.dependency._file_base import FileMutatingTool
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import FileChange, ToolError, ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise/return site).
_MSG_FILE_PATH_REQUIRED = "Error: 'file_path' argument is required."
_MSG_CONTENT_NOT_STRING = "Error: 'content' must be a string."
_MSG_CONTENT_TOO_LARGE = "Error: content ({size} bytes) exceeds the maximum allowed size ({max_size} bytes)."
_MSG_IS_DIRECTORY = "Error: '{path}' is a directory, not a file. Provide a file path to write to."
_MSG_CANNOT_MKDIR = "Error: cannot create parent directory for '{path}': {error}"
_MSG_CANNOT_WRITE = "Error: cannot write '{path}': {error}"
_MSG_WRITE_OK = "{verb} {path} ({lines}, {size} bytes written)."


@register_tool
class Write(FileMutatingTool):
    """Write content to a file on the local filesystem (create or overwrite)."""

    name = "Write"
    aliases: ClassVar[list[str]] = ["Write.run", "write"]
    # The effect (file on disk) is durable and re-readable, so the success-message
    # body can be cleared without losing recoverable information.
    reconstructable: ClassVar[bool] = True
    # Success messages can echo file content; allow a higher cap.
    max_result_size_chars: ClassVar[int] = 100_000

    async def call(self, *, file_path: str, content: str = "") -> ToolResult:
        """Write a whole file to disk — create a new file or fully replace one.

        Writes a file to the local filesystem, creating any missing parent
        directories. If the file exists it is OVERWRITTEN; otherwise it is
        created. When overwriting, the existing file's newline style is
        preserved.

        - If the file already exists, you must use the Read tool on it first, so
          you are editing from its current contents rather than clobbering
          changes you have not seen (an existing file must have been read this
          session and be unchanged since before it can be overwritten).
        - ALWAYS prefer editing an existing file with the Edit tool when only
          part of it changes. Only use Write to create a new file or fully
          replace one.
        - NEVER proactively create documentation (*.md) or README files unless
          the user explicitly asks for them.

        Args:
            file_path: Absolute path to the file to write (~ is expanded;
                relative paths resolve against the current working directory).
            content: The full text content to write to the file.
        """
        if not file_path or not file_path.strip():
            raise ToolError(_MSG_FILE_PATH_REQUIRED)

        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ToolError(_MSG_CONTENT_NOT_STRING)

        encoded_size = len(content.encode("utf-8"))
        if encoded_size > MAX_CONTENT_SIZE_BYTES:
            raise ToolError(_MSG_CONTENT_TOO_LARGE.format(size=encoded_size, max_size=MAX_CONTENT_SIZE_BYTES))

        full_path = self._resolve_path(file_path.strip())

        if os.path.isdir(full_path):
            raise ToolError(_MSG_IS_DIRECTORY.format(path=file_path))

        existed = os.path.exists(full_path)

        # Read-before-overwrite: an existing file must have been read this
        # session and not changed on disk since. Skipped for new files and when
        # the capability isn't injected (unbound use).
        old = ""
        if existed:
            self._check_read_before_write(file_path, full_path, noun="file", verb="overwriting")
            # Capture the before-image (LF-normalized — the display-agnostic
            # form) so the result can carry the change as a structured
            # ``FileChange`` fact, exactly like Edit. Unreadable → treat as
            # empty so the write still proceeds and simply shows as a full add.
            try:
                with open(full_path, "r", encoding="utf-8", newline="") as f:
                    old = f.read().replace("\r\n", "\n")
            except (OSError, UnicodeDecodeError):
                old = ""

        # Preserve the existing newline style on overwrite; default to LF for
        # new files. Content arrives normalized to "\n"; translate on write.
        line_ending = self._detect_line_ending(full_path) if existed else "\n"

        parent = os.path.dirname(full_path)
        if parent and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                raise ToolError(_MSG_CANNOT_MKDIR.format(path=file_path, error=e))

        # Capture a before-image for file history (undo/diff) just before we
        # overwrite. No-op when unbound; best-effort (never blocks the write).
        self._snapshot_pre_write(full_path)

        try:
            # newline="" disables Python's own translation; we translate
            # explicitly so the detected line ending is honored exactly.
            normalized = content
            if line_ending != "\n":
                normalized = content.replace("\r\n", "\n").replace("\n", line_ending)
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(normalized)
        except OSError as e:
            raise ToolError(_MSG_CANNOT_WRITE.format(path=file_path, error=e))

        # Refresh the shared file-read state to the just-written content, so a
        # subsequent Write/Edit to the same file isn't blocked as "modified
        # since read" by our own write. No-op when unbound.
        self._refresh_read_state(full_path)

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        verb = "Updated" if existed else "Created"
        message = _MSG_WRITE_OK.format(
            verb=verb, path=full_path, lines=count_noun(line_count, "line"), size=encoded_size
        )
        # Carry the change as a structured fact (old/new full content, LF-normalized)
        # so the view layer renders the full file content — a selectable diff on a
        # text host — instead of a bare one-line summary, matching Edit.
        return ToolResult(
            output=message,
            file_changes=[FileChange(path=full_path, old=old, new=content.replace("\r\n", "\n"))],
        )
