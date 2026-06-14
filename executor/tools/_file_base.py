"""FileMutatingTool — shared base for tools that modify files on disk.

Write, Edit, and NotebookEdit all share the same cross-cutting concerns:

- They need the Role's shared file-read state to enforce *read-before-write*
  (an existing file must have been read this session and be unchanged on disk
  since), and to refresh that state after writing so a follow-up edit isn't
  wrongly blocked as "modified since read".
- They preserve the existing file's newline style (LF vs CRLF) on write.

This base collects those helpers so each tool body stays focused on its own
edit semantics. It is intentionally NOT decorated with @register_tool — it is an
abstract base, not a usable tool, so the registry's package scan ignores it.

All guards are no-ops when the tool is used unbound (no Role injected the
capabilities), so the concrete tools keep working standalone and in tests.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_result import ToolError

# How many bytes to sniff when detecting an existing file's newline style.
_SNIFF_BYTES = 64 * 1024


class FileMutatingTool(BaseTool):
    """Base for file-mutating tools (Write/Edit/NotebookEdit).

    Provides read-before-write enforcement, post-write read-state refresh, and
    newline-style detection against the Role's shared file-read state. Concrete
    subclasses still set their own name/aliases/description and implement call().
    """

    # Read-before-write needs both shared file-read capabilities. bind() injects
    # only these; absent (unbound) they stay unset and the guards self-skip.
    requires = ("get_file_read_mtime", "record_file_read")

    # Injected from Role by bind().
    get_file_read_mtime: Callable[[str], Optional[int]]
    record_file_read: Callable[[str, int], None]

    @staticmethod
    def _detect_line_ending(full_path: str) -> str:
        """Return the dominant newline style ("\\n" or "\\r\\n") of an existing file.

        Reads a prefix in binary and compares CRLF vs bare-LF counts. Defaults to
        "\\n" when the file is empty, missing, or unreadable.
        """
        try:
            with open(full_path, "rb") as f:
                chunk = f.read(_SNIFF_BYTES)
        except OSError:
            return "\n"
        crlf = chunk.count(b"\r\n")
        lf = chunk.count(b"\n") - crlf
        return "\r\n" if crlf > lf else "\n"

    def _check_read_before_write(
        self,
        display_path: str,
        full_path: str,
        *,
        noun: str = "file",
        verb: str = "writing to",
    ) -> None:
        """Guard mutating an existing file. Raises ToolError to abort, or returns
        None to allow the operation.

        Requires that the file was read this session (recorded in the Role's
        shared file-read state) and has not changed on disk since that read.
        Skipped (returns None) when the capability isn't injected (unbound use)
        or when the file can't be stat'd.

        Args:
            display_path: Path as the model referred to it, used in messages.
            full_path: Resolved absolute path, used for the actual lookup/stat.
            noun: How to refer to the target in messages ("file"/"notebook").
            verb: The action being guarded, used in the not-read message
                ("writing to"/"editing"/"overwriting").
        """
        getter = getattr(self, "get_file_read_mtime", None)
        if getter is None:
            return  # unbound: no shared state to consult

        read_mtime = getter(full_path)
        if read_mtime is None:
            raise ToolError(
                f"Error: {noun} '{display_path}' has not been read this session. "
                f"Use the Read tool to read it first before {verb} it — this "
                f"prevents changing content you haven't seen."
            )
        try:
            current_mtime = os.stat(full_path).st_mtime_ns
        except OSError:
            return  # can't stat; let the read/write attempt surface the error
        if current_mtime != read_mtime:
            raise ToolError(
                f"Error: {noun} '{display_path}' has been modified since it was "
                f"last read. Read it again before {verb} it."
            )

    def _refresh_read_state(self, full_path: str) -> None:
        """Refresh the shared file-read state to the just-written file so a
        subsequent Write/Edit to the same file isn't blocked as "modified since
        read" by our own write. No-op when unbound.
        """
        recorder = getattr(self, "record_file_read", None)
        if recorder is not None:
            try:
                recorder(full_path, os.stat(full_path).st_mtime_ns)
            except OSError:
                pass
