"""Search tool — unified file-name + content search built on ripgrep.

A single search primitive over two ORTHOGONAL, independently-optional axes:

- ``files`` (glob syntax, e.g. "**/*.py") selects WHICH files.
- ``content`` (regex syntax, e.g. "def \\w+") matches WHAT content.

They compose rather than compete, so one tool subsumes both name-search and
content-search without a mode switch:

- ``files`` only            -> list files by name (sorted by recency).
- ``content`` only          -> search file contents under ``path``.
- ``files`` + ``content``   -> search ``content`` only within globbed files.
- neither                   -> error (at least one axis is required).

Each axis keeps its native, best-fit language — glob for paths (``**/*.ts``),
regex for content (``\\bTODO\\b``) — deliberately NOT collapsed into one
grammar. Built on the ripgrep binary (`rg`), with a pure-Python glob fallback
for the file-listing axis when ripgrep is absent. Content search additionally
reads rich documents (PDF/.docx/.xlsx) via a text-extraction pass. VCS metadata
directories are excluded automatically; results are relativized to the working
directory to save tokens; a large result is persisted to disk by the shared
tool-result exit rather than truncated here.

The model-facing ``output`` text is unchanged; alongside it the result carries a
structured ``data = {"files": [<absolute path>, ...]}`` — the deduplicated set of
files the query matched/listed, in output order. This lets a downstream consumer
(e.g. a ``run_graph`` map that fans a per-file edit out over the hits) index the
file list directly via ``$ref`` instead of re-parsing the ``path:line`` text.
"""
from __future__ import annotations

import asyncio
import fnmatch
import glob as _glob_mod
import os
import re
import time
from typing import ClassVar, Optional

from mote.common.const.tools import (
    DOCUMENT_EXTENSIONS,
    GLIMPSE_EXTENSIONS,
    GLIMPSE_RECORD_LIMIT,
    SEARCH_TIMEOUT,
    VCS_DIRECTORIES_TO_EXCLUDE,
)
from mote.common.schema import ToolEffect
from mote.common.text import count_noun, display_path, plural
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import GetCwd, RecordFileGlimpsed
from mote.executor.dependency._document import extract_document_text as _extract_document_text
from mote.executor.dependency._document import is_document as _is_document
from mote.executor.dependency._paths import base_cwd, resolve_path
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError, ToolResult
from mote.executor.tools._search_engine import (
    DOC_ONLY_TYPES,
    HEAVY_DIRECTORIES_TO_EXCLUDE,
    TYPE_EXTENSIONS,
    VENDORED_RIPGREP,
    apply_head_limit,
    find_ripgrep,
    split_glob,
)

# Complete model-facing message sentences, hoisted to module-top templates so
# the wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_NO_AXIS = "Error: provide 'files' (a glob) and/or 'content' (a regex) — at least one is required."
_MSG_INVALID_OUTPUT_MODE = (
    "Error: invalid output_mode '{output_mode}'. Must be one of 'files_with_matches', 'content', 'count'."
)
_MSG_PATH_NOT_FOUND = (
    "Error: path does not exist: {path}. The path should be absolute or "
    "relative to the working directory ({base_cwd})."
)
_MSG_NOT_A_DIRECTORY = "Error: path is not a directory: {path}"
_MSG_RIPGREP_MISSING = (
    "Error: ripgrep (rg) is required for content search but was not found. "
    "Install ripgrep or ensure the vendored binary is present at {vendored}."
)
_MSG_SEARCH_TIMEOUT = "Error: search timed out after {seconds:.0f}s. Try a more specific path or pattern."
_MSG_INVALID_REGEX = "Error: invalid regular expression '{pattern}': {error}"
_MSG_SEARCH_FAILED = "Error running search: {error}"
_MSG_NO_FILES = "No files found"
_MSG_NO_MATCHES = "No matches found"


def _mtime(p: str) -> float:
    """File mtime (epoch secs), 0.0 when unstattable — for recency ordering."""
    try:
        return os.stat(p).st_mtime
    except OSError:
        return 0.0


@register_tool
class Search(BaseTool):
    """Search files by name (glob) and/or contents (regex), in one call."""

    name = "Search"
    # Search subsumes the retired Grep/Glob tools, so it inherits their dispatch
    # names too — a rollout / config / history that still says "grep" or "glob"
    # keeps resolving here (one tool, all the old entry points).
    aliases: ClassVar[list[str]] = ["Search.run", "search", "grep", "glob"]
    keywords: ClassVar[list[str]] = [
        "find",
        "locate",
        "ripgrep",
        "rg",
        "file",
        "content",
        "pattern",
        "查找",
        "搜索",
        "文件",
        "内容",
        "正则",
    ]
    # Read-only observation: results are re-derivable by re-running the query.
    reconstructable: ClassVar[bool] = True
    # No side effect — opt out of the effect ledger (safe to replay always).
    effect: ClassVar[ToolEffect] = ToolEffect.PURE
    # Can list many paths / matches; allow a higher cap before persisting.
    max_result_size_chars: ClassVar[int] = 100_000
    # get_cwd is the stable base for the default search root + output
    # relativization. record_file_glimpsed feeds matched files to the code map
    # as navigation hints. Both optional: unbound (no Role) falls back / no-ops.
    requires = ("get_cwd", "record_file_glimpsed")

    # Injected from Role by bind(): Role.get_cwd, Role.record_file_glimpsed.
    get_cwd: GetCwd
    record_file_glimpsed: RecordFileGlimpsed

    def _base_cwd(self) -> str:
        """The stable base dir for default root / relativization (unbound: cwd)."""
        return base_cwd(getattr(self, "get_cwd", None))

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
        head_limit: Optional[int] = None,
        offset: int = 0,
    ) -> ToolResult:
        """Find files by name and/or search their contents — one unified tool.

        Two orthogonal axes, at least one required; they compose:
        - files (GLOB) selects WHICH files, e.g. "**/*.py".
        - content (REGEX) matches WHAT to find inside them, e.g. "def \\w+".
        files alone lists files by name; content alone searches every file under
        `path`; both search content only within globbed files.

        Prefer this over find/ls/grep/rg via Bash. Content search reads
        PDF/.docx/.xlsx too and returns 'path:line' rows you can pass straight to
        Read's offset. output_mode/context/line_numbers/multiline apply to content
        search only. Also returns data={"files": [<abs path>, ...]} (deduped, in
        output order) so an orchestration can drive a per-file step over the hits.

        Args:
            files: Glob selecting which files, e.g. "**/*.py" or "*.{ts,tsx}".
                Comma/space-separate multiple patterns. Omit to search all under `path`.
            content: Regex to match inside file contents. Omit for a pure name search.
            path: Directory or file to anchor the search. Defaults to the cwd.
                IMPORTANT: omit to use the default; do NOT pass "undefined"/"null".
            type: Restrict to a file type, e.g. "py"/"js"/"go" or a document type
                "pdf"/"docx"/"word"/"xlsx"/"excel"/"csv". More efficient than
                `files` for standard types; combines with it. Applies to content search.
            output_mode: Content search only. "files_with_matches" (default; one
                "path:line" per file, line = first match — feed to Read's offset),
                "content" (matching lines as "path:line:text"), or "count".
            case_insensitive: Case-insensitive search (ripgrep -i).
            line_numbers: Show line numbers in content mode (default True; ignored
                unless output_mode="content").
            before_context: Lines of context before each match (rg -B; content mode).
            after_context: Lines of context after each match (rg -A; content mode).
            context: Context before AND after each match (rg -C; precedes
                before/after; content mode only).
            multiline: Let patterns span lines, "." also matching newlines
                (ripgrep -U --multiline-dotall). Default False.
            head_limit: Limit output to the first N entries (like "| head -N").
                Omit or 0 for all results.
            offset: Skip the first N entries before head_limit (pagination). Default 0.
        """
        files = files.strip()
        content = content.strip()
        if not files and not content:
            raise ToolError(_MSG_NO_AXIS)
        if content and output_mode not in ("files_with_matches", "content", "count"):
            raise ToolError(_MSG_INVALID_OUTPUT_MODE.format(output_mode=output_mode))

        base = self._base_cwd()
        root = resolve_path(getattr(self, "get_cwd", None), path.strip()) if path.strip() else base
        if path.strip() and not os.path.exists(root):
            raise ToolError(_MSG_PATH_NOT_FOUND.format(path=path, base_cwd=base))

        if not content:
            text, matched = await self._list_files(root, files, base)
        else:
            text, matched = await self._search_content(
                root,
                base,
                files,
                content,
                type,
                output_mode,
                case_insensitive,
                line_numbers,
                before_context,
                after_context,
                context,
                multiline,
                head_limit,
                offset,
            )
        return ToolResult(output=text, data={"files": matched})

    # ------------------------------------------------------------------
    # File-listing axis (glob only) — subsumes the legacy Glob tool.
    # ------------------------------------------------------------------

    async def _list_files(self, root: str, pattern: str, base: str) -> tuple[str, list[str]]:
        """List files matching `pattern` under `root`, sorted by recency.

        Returns the rendered text plus the ordered absolute paths (the same set
        the text lists, for the structured ``data`` file list).

        `root` is already verified to exist when the caller passed an explicit
        `path`; here we only reject a non-directory root (a `files`-only query
        against a single file makes no sense).
        """
        if not os.path.isdir(root):
            raise ToolError(_MSG_NOT_A_DIRECTORY.format(path=root))
        rg = find_ripgrep()
        try:
            if rg is not None:
                found = await self._rg_files(rg, root, pattern)
            else:
                found = self._py_files(root, pattern)
        except TimeoutError:
            raise ToolError(_MSG_SEARCH_TIMEOUT.format(seconds=SEARCH_TIMEOUT))
        except Exception as e:  # noqa: BLE001 — surface the failure to the model
            raise ToolError(_MSG_SEARCH_FAILED.format(error=e))
        self._record_glimpses(found, is_rows=False)
        ordered = sorted(found, key=lambda p: (-_mtime(p), p))
        abs_files = [os.path.abspath(p) for p in ordered]
        if not ordered:
            return _MSG_NO_FILES, abs_files
        return "\n".join(display_path(p, base) for p in ordered), abs_files

    async def _rg_files(self, rg: str, root: str, pattern: str) -> list[str]:
        """List files under root matching a glob via `rg --files`, abs paths.

        Runs with cwd=root and no path argument so ripgrep's --glob matching is
        anchored relative to root; otherwise a path-relative glob such as
        "tools/*.py" would not match. An empty pattern lists every file.

        Exit code 0 = matches, 1 = no matches (both fine); anything else raises.
        """
        args = [rg, "--files", "--hidden"]
        if pattern:
            args += ["--glob", pattern]
        for vcs in VCS_DIRECTORIES_TO_EXCLUDE:
            args += ["--glob", f"!{vcs}"]

        proc = await asyncio.create_subprocess_exec(
            *args, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SEARCH_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError
        if proc.returncode not in (0, 1):
            msg = stderr.decode(errors="replace").strip()
            raise RuntimeError(msg or f"ripgrep exited with code {proc.returncode}")
        out = []
        for ln in stdout.decode(errors="replace").split("\n"):
            ln = ln.rstrip("\r")
            if not ln:
                continue
            out.append(ln if os.path.isabs(ln) else os.path.join(root, ln))
        return out

    @staticmethod
    def _py_files(root: str, pattern: str) -> list[str]:
        """Pure-Python fallback using the glob module (supports ** recursion).

        Honors the VCS metadata exclusions but NOT .gitignore. Hidden files are
        included (include_hidden=True) to match ripgrep's --hidden. An empty
        pattern lists every file.

        ripgrep's --glob treats a separator-less pattern (e.g. "*.py") as a
        basename match at ANY depth, whereas glob.glob anchors it to the top
        level. To match ripgrep, such patterns are searched recursively.
        """
        pat = pattern or "**/*"
        search_pattern = pat if "/" in pat else os.path.join("**", pat)
        full_pattern = os.path.join(root, search_pattern)
        excluded = set(VCS_DIRECTORIES_TO_EXCLUDE)
        out = []
        for p in _glob_mod.glob(full_pattern, recursive=True, include_hidden=True):
            if not os.path.isfile(p):
                continue
            parts = os.path.relpath(p, root).split(os.sep)
            if any(part in excluded for part in parts):
                continue
            out.append(p)
        return out

    # ------------------------------------------------------------------
    # Content axis (regex, optional glob/type filter) — subsumes legacy Grep.
    # ------------------------------------------------------------------

    async def _search_content(
        self,
        root,
        base,
        files,
        content,
        type_,
        output_mode,
        case_insensitive,
        line_numbers,
        before_context,
        after_context,
        context,
        multiline,
        head_limit,
        offset,
    ) -> tuple[str, list[str]]:
        rg = find_ripgrep()
        # A doc-only type (pdf/docx/xlsx/...) has no ripgrep --type and matches
        # only rich documents, so skip the ripgrep text pass entirely for it.
        doc_only = type_ in DOC_ONLY_TYPES
        # The document-extraction pass walks the tree in-process, so only run it
        # when the query actually targets documents (rg handles everything else).
        want_documents = doc_only or self._targets_documents(root, files, type_)
        # Wall-clock deadline for the (synchronous) document pass. It runs in a
        # worker thread so it never blocks the event loop, and honors this
        # deadline so a huge tree can't run unbounded.
        deadline = time.monotonic() + SEARCH_TIMEOUT
        try:
            rows: list[str] = []
            # ripgrep is the sole text-search engine (no in-process fallback).
            if not doc_only:
                if rg is None:
                    raise ToolError(_MSG_RIPGREP_MISSING.format(vendored=VENDORED_RIPGREP))
                rows = await self._rg_content(
                    rg,
                    root,
                    content,
                    files,
                    type_,
                    output_mode,
                    case_insensitive,
                    line_numbers,
                    before_context,
                    after_context,
                    context,
                    multiline,
                )
            # ripgrep can't read PDF/Word/Excel (binary or zipped XML). When the
            # query targets documents, run a separate extraction pass and merge —
            # no overlap, since rg never matched those files. Synchronous, so
            # offload it to a thread to keep the loop free.
            if want_documents:
                rows += await asyncio.to_thread(
                    self._doc_content,
                    root,
                    content,
                    files,
                    type_,
                    output_mode,
                    case_insensitive,
                    line_numbers,
                    multiline,
                    deadline,
                )
        except TimeoutError:
            raise ToolError(_MSG_SEARCH_TIMEOUT.format(seconds=SEARCH_TIMEOUT))
        except re.error as e:
            raise ToolError(_MSG_INVALID_REGEX.format(pattern=content, error=e))
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 — surface the failure to the model
            raise ToolError(_MSG_SEARCH_FAILED.format(error=e))

        self._record_glimpses(rows, is_rows=True)
        return self._format(rows, root, base, output_mode, head_limit, offset)

    @staticmethod
    def _distinct_files(rows: list[str]) -> list[str]:
        """Absolute paths named by content rows, deduplicated, in first-seen order.

        Content rows begin ``<abs path>:...`` (files_with_matches "path:lineno",
        count "path:count", content "path:line:text"), so the path is the prefix
        up to the first ``:``. One path may appear on many rows (multiple matches
        in a file) — collapse to the distinct set so the structured ``data`` file
        list mirrors the "N files" the header reports.
        """
        seen: set[str] = set()
        out: list[str] = []
        for row in rows:
            path = row.split(":", 1)[0]
            if not path or path in seen:
                continue
            seen.add(path)
            out.append(os.path.abspath(path))
        return out

    async def _rg_content(
        self,
        rg,
        root,
        pattern,
        files,
        type_,
        output_mode,
        case_insensitive,
        line_numbers,
        before_context,
        after_context,
        context,
        multiline,
    ) -> list[str]:
        """Run the ripgrep binary and return its stdout lines (no trailing CR).

        Exit code 0 = matches, 1 = no matches (both fine); anything else raises.
        """
        args = [rg, "--hidden"]
        for vcs in VCS_DIRECTORIES_TO_EXCLUDE:
            args += ["--glob", f"!{vcs}"]
        if multiline:
            args += ["-U", "--multiline-dotall"]
        if case_insensitive:
            args.append("-i")
        if output_mode == "files_with_matches":
            # Emit the first match per file WITH its line number (-n -H -m 1) so
            # the caller can Read at that offset, then strip the text below.
            args += ["-n", "-H", "-m", "1"]
        elif output_mode == "count":
            args.append("-c")
        elif output_mode == "content":
            if line_numbers:
                args.append("-n")
            if context:
                args += ["-C", str(context)]
            else:
                if before_context:
                    args += ["-B", str(before_context)]
                if after_context:
                    args += ["-A", str(after_context)]
        if type_:
            args += ["--type", type_]
        for gp in split_glob(files):
            args += ["--glob", gp]
        # Pattern last; use -e so a leading dash isn't read as a flag.
        args += ["-e", pattern, root]

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SEARCH_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError
        if proc.returncode not in (0, 1):
            msg = stderr.decode(errors="replace").strip()
            raise RuntimeError(msg or f"ripgrep exited with code {proc.returncode}")
        text = stdout.decode(errors="replace")
        lines = [ln.rstrip("\r") for ln in text.split("\n") if ln]
        if output_mode == "files_with_matches":
            # rg emitted "<path>:<lineno>:<text>" (-n -H -m 1). Keep just
            # "<path>:<lineno>" — the position is what Read needs as an offset.
            stripped = []
            for ln in lines:
                path, _, rest = ln.partition(":")
                lineno, _, _ = rest.partition(":")
                stripped.append(f"{path}:{lineno}" if lineno else ln)
            return stripped
        return lines

    @staticmethod
    def _targets_documents(root: str, files: str, type_: str) -> bool:
        """Whether this query should trigger the (in-process) document pass.

        The document pass walks the tree in Python, which is expensive, so it
        only runs when the query actually targets rich documents:
        - a rich-document ``type`` (pdf/docx/xlsx/word/excel), or
        - a ``files`` glob that names a document extension (``*.pdf``,
          ``*.{docx,xlsx}``), or
        - a search root that is itself a document file.
        Otherwise (the overwhelmingly common code-search case) it is skipped.
        """
        if type_ in DOC_ONLY_TYPES:
            return True
        if os.path.isfile(root):
            return _is_document(root)
        # Match a bare extension (".pdf" -> "pdf") anywhere in a glob pattern so
        # both "*.pdf" and brace groups like "*.{docx,xlsx}" are recognized.
        hints = tuple(ext.lstrip(".") for ext in DOCUMENT_EXTENSIONS)
        for pat in split_glob(files):
            low = pat.lower()
            if any(h in low for h in hints):
                return True
        return False

    def _doc_content(
        self, root, pattern, files, type_, output_mode, case_insensitive, line_numbers, multiline, deadline=None
    ) -> list[str]:
        """Search rich documents (PDF/Word/Excel) by extracting their text first.

        Each document's extracted text is matched with the same regex and fed
        through _collect, so output is ripgrep-shaped and the existing
        sort/limit/format logic applies unchanged. Documents whose format has no
        available extractor (missing optional dep) are silently skipped, exactly
        like ripgrep skips binaries. Raises TimeoutError once ``deadline`` (a
        time.monotonic() value) passes.
        """
        flags = re.MULTILINE
        if case_insensitive:
            flags |= re.IGNORECASE
        if multiline:
            flags |= re.DOTALL
        regex = re.compile(pattern, flags)

        globs = split_glob(files)
        type_exts = TYPE_EXTENSIONS.get(type_, ()) if type_ else ()
        rows: list[str] = []

        for file_path in self._walk_files(root):
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError
            if not _is_document(file_path):
                continue
            if not self._file_matches_filters(file_path, root, globs, type_exts):
                continue
            data = _extract_document_text(file_path)
            if not data:
                continue
            if regex.search(data):
                self._collect(rows, output_mode, file_path, data, regex, line_numbers, multiline=multiline)
        return rows

    @staticmethod
    def _walk_files(root: str):
        """Yield file paths under root, pruning VCS metadata and heavy
        dependency/build directories (node_modules, .venv, __pycache__, ...).

        The Python passes don't read .gitignore, so these are pruned explicitly;
        otherwise a tree with many node_modules/.venv would take effectively
        forever to walk.
        """
        if os.path.isfile(root):
            yield root
            return
        excluded = set(VCS_DIRECTORIES_TO_EXCLUDE) | HEAVY_DIRECTORIES_TO_EXCLUDE
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in excluded]
            for name in filenames:
                yield os.path.join(dirpath, name)

    @staticmethod
    def _file_matches_filters(file_path, root, globs, type_exts) -> bool:
        """Apply glob and --type filters to a single file path."""
        if type_exts and not file_path.endswith(type_exts):
            return False
        if globs:
            base = os.path.basename(file_path)
            rel = os.path.relpath(file_path, root)
            if not any(fnmatch.fnmatch(base, g) or fnmatch.fnmatch(rel, g) for g in globs):
                return False
        return True

    @staticmethod
    def _collect(rows, output_mode, file_path, data, regex, line_numbers, multiline) -> None:
        """Append ripgrep-shaped rows for one matching file."""
        if output_mode == "files_with_matches":
            # Record the first match's line number so callers can Read with an
            # offset that lands on (or just before) the match. Works for both
            # single-line and multiline patterns via the match's byte offset.
            m = regex.search(data)
            lineno = data.count("\n", 0, m.start()) + 1 if m else 1
            rows.append(f"{file_path}:{lineno}")
            return
        if output_mode == "count":
            n = len(regex.findall(data))
            rows.append(f"{file_path}:{n}")
            return
        # content mode
        if multiline:
            # Can't attribute multiline matches to single line numbers reliably;
            # emit the matched spans intact (a large result is persisted to disk
            # by the shared tool-result exit rather than truncated here).
            for m in regex.finditer(data):
                snippet = m.group(0).replace("\n", "\\n")
                rows.append(f"{file_path}:{snippet}")
            return
        for i, line in enumerate(data.splitlines(), start=1):
            if regex.search(line):
                rows.append(f"{file_path}:{i}:{line}" if line_numbers else f"{file_path}:{line}")

    # ------------------------------------------------------------------
    # Shared: code-map glimpses + result formatting.
    # ------------------------------------------------------------------

    def _record_glimpses(self, items: list[str], *, is_rows: bool) -> None:
        """Feed matched ``.py`` files to the code map as glimpse hints (P2).

        Best-effort navigation aid: records at most
        :data:`GLIMPSE_RECORD_LIMIT` matched ``.py`` files so the code map can
        surface their structure to guide "which of these should I open".
        ``is_rows`` distinguishes the two callers: content rows begin
        ``<abs path>:...`` (split on the first ``:``, dedup, keep result order),
        while the file-listing axis passes bare paths (mtime-sorted). Unbound
        (no Role) → the capability is absent → no-op. A raising sink is
        swallowed (a navigation hint must never fail a search).
        """
        record = getattr(self, "record_file_glimpsed", None)
        if record is None:
            return
        if is_rows:
            paths: list[str] = []
            seen: set[str] = set()
            for row in items:
                path = row.split(":", 1)[0]
                if not path.endswith(GLIMPSE_EXTENSIONS) or path in seen:
                    continue
                seen.add(path)
                paths.append(path)
        else:
            py = [p for p in items if p.endswith(GLIMPSE_EXTENSIONS)]
            paths = sorted(py, key=lambda p: (-_mtime(p), p))
        for p in paths[:GLIMPSE_RECORD_LIMIT]:
            try:
                record(os.path.abspath(p))
            except Exception:  # noqa: BLE001 — advisory; never fail the search
                return

    def _format(self, rows, root, base, output_mode, head_limit, offset) -> tuple[str, list[str]]:
        """Render content-search rows into the (text, matched-files) result.

        The file list mirrors what the text shows: it is derived from the SAME
        rows after head_limit/offset pagination, so ``data["files"]`` and the
        rendered header agree on which files the query surfaced.
        """
        if output_mode == "files_with_matches":
            return self._format_files(rows, base, head_limit, offset)
        if output_mode == "count":
            return self._format_count(rows, base, head_limit, offset)
        return self._format_content(rows, base, head_limit, offset)

    def _format_files(self, rows, base, head_limit, offset) -> tuple[str, list[str]]:
        # Rows are "<path>:<lineno>" (lineno = first match, for Read offset).
        # Sort by mtime (most recent first), filename as tiebreaker.
        def parse(row: str) -> tuple[str, str]:
            path, _, lineno = row.rpartition(":")
            return (path, lineno) if path and lineno.isdigit() else (row, "")

        parsed = [parse(r) for r in rows]
        ordered = sorted(parsed, key=lambda pl: (-_mtime(pl[0]), pl[0]))
        limited, applied = apply_head_limit(ordered, head_limit, offset)
        # One row per matching file here, but dedup defensively so the file list
        # is always distinct.
        matched = self._distinct_files([path for path, _ in limited])
        if not limited:
            return _MSG_NO_FILES, matched
        lines = [
            f"{display_path(path, base)}:{lineno}" if lineno else display_path(path, base) for path, lineno in limited
        ]
        header = f"Found {count_noun(len(lines), 'file')}{self._pagination(applied, offset)}"
        return header + "\n" + "\n".join(lines), matched

    def _format_count(self, rows, base, head_limit, offset) -> tuple[str, list[str]]:
        limited, applied = apply_head_limit(rows, head_limit, offset)
        total = 0
        files = 0
        out_lines = []
        matched: list[str] = []
        for line in limited:
            head, _, count_str = line.rpartition(":")
            try:
                count = int(count_str)
            except ValueError:
                out_lines.append(line)
                continue
            total += count
            files += 1
            out_lines.append(f"{display_path(head, base)}:{count}")
            matched.append(os.path.abspath(head))
        body = "\n".join(out_lines) if out_lines else _MSG_NO_MATCHES
        summary = (
            f"\n\nFound {total} total {plural('occurrence', total)} across "
            f"{count_noun(files, 'file')}"
            f"{self._pagination(applied, offset)}"
        )
        return body + summary, matched

    def _format_content(self, rows, base, head_limit, offset) -> tuple[str, list[str]]:
        limited, applied = apply_head_limit(rows, head_limit, offset)
        # Many rows can share a file (multiple matching lines) — collapse to the
        # distinct files surfaced within the paginated window.
        matched = self._distinct_files(limited)
        if not limited:
            return _MSG_NO_MATCHES, matched
        out = []
        for line in limited:
            # Lines are "<abs path>:<rest>"; relativize the path prefix only.
            idx = line.find(":")
            if idx > 0:
                out.append(display_path(line[:idx], base) + line[idx:])
            else:
                out.append(line)
        body = "\n".join(out)
        pag = self._pagination(applied, offset)
        return body + (f"\n\n[Showing results with pagination ={pag.lstrip(' ')}]" if pag else ""), matched

    @staticmethod
    def _pagination(applied_limit, offset) -> str:
        parts = []
        if applied_limit is not None:
            parts.append(f"limit: {applied_limit}")
        if offset:
            parts.append(f"offset: {offset}")
        return f" ({', '.join(parts)})" if parts else ""
