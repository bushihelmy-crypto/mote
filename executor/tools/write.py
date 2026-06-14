"""Write file tool — aligned with Claude Code's Write (FileWriteTool).

Writes ``content`` to a file on the local filesystem, creating the file (and any
missing parent directories) or overwriting it if it already exists. The result
reports whether the file was created or updated and how many lines/bytes were
written, mirroring CC so model behavior stays familiar.

Differences from Claude Code's tool, by design:
- Read-before-overwrite is enforced via the Role's shared file-read state
  (Role.get_file_read_mtime, the analogue of CC's readFileState): an existing
  file must have been read this session, and must not have changed on disk since
  that read, before it can be overwritten. When the tool is used unbound (no
  Role injected the capability), the check is skipped so it still works in
  isolation/tests.
- When overwriting, the existing file's newline style (LF vs CRLF) and text
  encoding are detected and preserved, the same way CC round-trips line endings,
  so writes don't silently rewrite every line of a CRLF file.
"""
from __future__ import annotations

import os
from typing import ClassVar

from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.executor.tools._file_base import FileMutatingTool
from metagpt.common.const.tools import MAX_CONTENT_SIZE_BYTES


@register_tool
class Write(FileMutatingTool):
    """Write content to a file on the local filesystem (create or overwrite)."""

    name = "Write"
    aliases: ClassVar[list[str]] = ["Write.run", "write"]
    # Success messages can echo file content; allow a higher cap (CC).
    max_result_size_chars: ClassVar[int] = 100_000
    description = (
        "Write a file to the local filesystem. Creates the file and any missing "
        "parent directories, or overwrites it if it already exists. Prefer "
        "editing an existing file over rewriting it when only part changes."
    )

    async def call(self, *, file_path: str, content: str = "") -> str:
        """Write content to a file on the local filesystem.

        If the file exists it is overwritten; otherwise it (and any missing
        parent directories) is created. When overwriting, the existing file's
        newline style is preserved. An existing file must have been read this
        session (and be unchanged since) before it can be overwritten.

        Args:
            file_path: Absolute path to the file to write (~ is expanded;
                relative paths resolve against the current working directory).
            content: The full text content to write to the file.
        """
        if not file_path or not file_path.strip():
            raise ToolError("Error: 'file_path' argument is required.")

        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ToolError("Error: 'content' must be a string.")

        encoded_size = len(content.encode("utf-8"))
        if encoded_size > MAX_CONTENT_SIZE_BYTES:
            raise ToolError(
                f"Error: content ({encoded_size} bytes) exceeds the maximum "
                f"allowed size ({MAX_CONTENT_SIZE_BYTES} bytes)."
            )

        full_path = os.path.abspath(os.path.expanduser(file_path.strip()))

        if os.path.isdir(full_path):
            raise ToolError(
                f"Error: '{file_path}' is a directory, not a file. Provide a "
                f"file path to write to."
            )

        existed = os.path.exists(full_path)

        # Read-before-overwrite: an existing file must have been read this
        # session and not changed on disk since. Skipped for new files and when
        # the capability isn't injected (unbound use).
        if existed:
            self._check_read_before_write(
                file_path, full_path, noun="file", verb="overwriting"
            )

        # Preserve the existing newline style on overwrite; default to LF for
        # new files. Content arrives normalized to "\n"; translate on write.
        line_ending = self._detect_line_ending(full_path) if existed else "\n"

        parent = os.path.dirname(full_path)
        if parent and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                raise ToolError(f"Error: cannot create parent directory for '{file_path}': {e}")

        try:
            # newline="" disables Python's own translation; we translate
            # explicitly so the detected line ending is honored exactly.
            normalized = content
            if line_ending != "\n":
                normalized = content.replace("\r\n", "\n").replace("\n", line_ending)
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(normalized)
        except OSError as e:
            raise ToolError(f"Error: cannot write '{file_path}': {e}")

        # Refresh the shared file-read state to the just-written content, so a
        # subsequent Write/Edit to the same file isn't blocked as "modified
        # since read" by our own write. No-op when unbound.
        self._refresh_read_state(full_path)

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        verb = "Updated" if existed else "Created"
        return (
            f"{verb} {full_path} ({line_count} line(s), {encoded_size} bytes "
            f"written)."
        )

