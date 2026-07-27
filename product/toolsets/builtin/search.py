"""Model-facing adapter for deterministic managed file search."""

from __future__ import annotations

import asyncio
from typing import ClassVar, Optional

from mote.contracts.fileops import FileOperationError, SearchOutputMode, SearchResult, SearchRow
from mote.contracts.text import count_noun, display_path, plural
from mote.contracts.tools.effects import ToolEffect
from mote.product.toolsets.constants import GLIMPSE_EXTENSIONS, GLIMPSE_RECORD_LIMIT, SEARCH_TIMEOUT
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import GetCwd, RecordFileGlimpsed, SearchFiles
from mote.runtime.tools.dependency._paths import base_cwd, resolve_path, resolve_permission_path
from mote.runtime.tools.tool_registry import register_tool
from mote.runtime.tools.tool_result import ToolError, ToolResult

_MSG_NO_AXIS = "Error: provide 'files' (a glob), 'content' (a regex), or 'cursor' " "to continue a previous search."
_MSG_INVALID_OUTPUT_MODE = (
    "Error: invalid output_mode '{output_mode}'. Must be one of "
    "'files_with_matches', 'content', 'count', or 'only_matching'."
)
_MSG_NO_FILES = "No files found"
_MSG_NO_MATCHES = "No matches found"


@register_tool
class Search(BaseTool):
    """Search file names and immutable file contents through File Operations."""

    name = "Search"
    aliases: ClassVar[list[str]] = ["Search.run", "search", "grep", "glob"]
    keywords: ClassVar[list[str]] = [
        "find",
        "locate",
        "file",
        "content",
        "pattern",
        "regex",
        "查找",
        "搜索",
        "文件",
        "内容",
        "正则",
    ]
    reconstructable: ClassVar[bool] = True
    effect: ClassVar[ToolEffect] = ToolEffect.PURE
    max_result_size_chars: ClassVar[int] = 100_000
    requires = ("get_cwd", "search_files", "record_file_glimpsed")

    get_cwd: GetCwd
    search_files: SearchFiles
    record_file_glimpsed: RecordFileGlimpsed

    def permission_target(self, args: dict) -> str:
        """The canonical live search root, or no path for artifact continuation."""
        cursor = args.get("cursor")
        if isinstance(cursor, str) and cursor.strip():
            return ""
        return resolve_permission_path(
            self.get_cwd,
            args.get("path", ""),
            default_to_cwd=True,
        )

    async def call(
        self,
        *,
        files: str = "",
        content: str = "",
        path: str = "",
        type: str = "",
        output_mode: str = "files_with_matches",
        case_insensitive: bool = False,
        line_numbers: bool = True,
        before_context: int = 0,
        after_context: int = 0,
        context: int = 0,
        multiline: bool = False,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
        head_limit: Optional[int] = None,
        offset: int = 0,
        cursor: Optional[str] = None,
    ) -> ToolResult:
        """Find files by glob and search sealed file snapshots with one regex.

        `files` selects candidate paths and `content` matches their extracted
        text. They may be used independently or together. Text files and
        PDF/DOCX/XLSX documents share the same regex, occurrence counting,
        skipped-file reporting, ordering, and cursor protocol. A cursor reads
        the next page from the immutable first-search artifact and never scans
        live files again.

        Args:
            files: Glob selecting files, such as "**/*.py" or "*.{ts,tsx}".
                Comma/space-separate multiple patterns.
            content: Python regular expression matched with MULTILINE semantics.
            path: Search root, relative to the session cwd or absolute.
            type: File type such as py/js/go/pdf/docx/xlsx.
            output_mode: "files_with_matches", "content", "count", or
                "only_matching".
            case_insensitive: Match without case distinctions.
            line_numbers: Include line numbers in rendered content rows.
            before_context: Context lines before each matching line.
            after_context: Context lines after each matching line.
            context: Set both before_context and after_context; when non-zero it
                takes precedence over those separate values.
            multiline: Let patterns span lines and make dot match newlines.
            encoding: Explicit text encoding, for example gbk or shift_jis.
            fallback_encoding: Encoding used only when strict detection rejects
                the bytes.
            head_limit: Maximum rows in this page. Omit or use 0 for the safe
                default page size; continue with next_cursor.
            offset: Initial stable row offset. Prefer cursor for later pages.
            cursor: Opaque next_cursor returned by an earlier call.
        """
        files = files.strip()
        content = content.strip()
        cursor = cursor.strip() if cursor else None
        if not files and not content and cursor is None:
            raise ToolError(_MSG_NO_AXIS)
        try:
            mode = SearchOutputMode(output_mode)
        except ValueError as exc:
            raise ToolError(_MSG_INVALID_OUTPUT_MODE.format(output_mode=output_mode)) from exc

        base = base_cwd(self.get_cwd)
        root = resolve_path(self.get_cwd, path.strip()) if path.strip() else base
        if context:
            before_context = context
            after_context = context

        try:
            result = await asyncio.to_thread(
                self.search_files,
                root=root,
                content=content,
                files=files,
                type_name=type.strip(),
                output_mode=mode,
                case_insensitive=case_insensitive,
                before_context=before_context,
                after_context=after_context,
                multiline=multiline,
                encoding=encoding,
                fallback_encoding=fallback_encoding,
                limit=head_limit,
                offset=offset,
                cursor=cursor,
                timeout=SEARCH_TIMEOUT,
            )
        except FileOperationError as exc:
            raise ToolError(f"Error: {exc}") from exc

        self._record_glimpses(result)
        return ToolResult(
            output=self._render(
                result,
                base=base,
                line_numbers=line_numbers,
            ),
            data=self._data(result),
        )

    def _record_glimpses(self, result: SearchResult) -> None:
        surfaced = (path.display for path in result.files if path.display.lower().endswith(GLIMPSE_EXTENSIONS))
        for path in tuple(surfaced)[:GLIMPSE_RECORD_LIMIT]:
            self.record_file_glimpsed(path)

    @staticmethod
    def _render(
        result: SearchResult,
        *,
        base: str,
        line_numbers: bool,
    ) -> str:
        if not result.rows:
            body = _MSG_NO_MATCHES if result.content_search else _MSG_NO_FILES
        elif not result.content_search:
            body = "\n".join(display_path(row.path.display, base) for row in result.rows)
        else:
            body = "\n".join(Search._render_row(row, base, result.output_mode, line_numbers) for row in result.rows)

        summary = result.summary
        details = (
            f"Scanned {count_noun(summary.scanned_files, 'file')}; "
            f"found {summary.total_occurrences} {plural('occurrence', summary.total_occurrences)} "
            f"across {count_noun(summary.matched_files, 'file')}"
        )
        if summary.skipped_files:
            details += f"; skipped {count_noun(summary.skipped_files, 'file')}"
        if not summary.complete:
            details += f"; scan incomplete ({summary.termination})"
        if result.next_cursor:
            details += "; more rows are available via data.next_cursor"
        return f"{body}\n\n{details}"

    @staticmethod
    def _render_row(
        row: SearchRow,
        base: str,
        mode: SearchOutputMode,
        line_numbers: bool,
    ) -> str:
        path = display_path(row.path.display, base)
        if mode == SearchOutputMode.COUNT:
            return f"{path}: {row.occurrence_count}"
        position = f":{row.line_number}" if line_numbers and row.line_number else ""
        if mode == SearchOutputMode.FILES_WITH_MATCHES:
            return f"{path}{position}"
        text = row.matched_text if mode == SearchOutputMode.ONLY_MATCHING else row.text
        text = text.replace("\r", "\\r").replace("\n", "\\n")
        marker = "context" if row.is_context else "match"
        return f"{path}{position} [{marker}] {text}"

    @staticmethod
    def _data(result: SearchResult) -> dict:
        return {
            "files": [path.display for path in result.files],
            "summary": {
                "discovered_files": result.summary.discovered_files,
                "scanned_files": result.summary.scanned_files,
                "matched_files": result.summary.matched_files,
                "total_occurrences": result.summary.total_occurrences,
                "skipped_files": result.summary.skipped_files,
                "complete": result.summary.complete,
                "termination": result.summary.termination,
            },
            "status": result.status.value,
            "skipped": [
                {
                    "path": skipped.path.display,
                    "reason": skipped.reason.value,
                    "detail": skipped.detail,
                }
                for skipped in result.skipped
            ],
            "skipped_truncated": result.skipped_truncated,
            "skipped_artifact": {
                "digest": result.skipped_artifact.digest,
                "size": result.skipped_artifact.size,
            },
            "next_cursor": result.next_cursor,
            "result_artifact": {
                "digest": result.artifact.digest,
                "size": result.artifact.size,
            },
        }


__all__ = ["Search"]
