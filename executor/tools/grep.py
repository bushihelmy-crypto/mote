"""Grep tool — aligned with Claude Code's Grep (GrepTool, built on ripgrep).

A powerful content search tool. Prefers the ripgrep binary (`rg`) when one is
available and falls back to a pure-Python walker otherwise, so it works even in
environments that don't ship ripgrep. The interface and defaults mirror Claude
Code's tool so model behavior stays familiar:

- Three output modes: ``files_with_matches`` (default), ``content``, ``count``.
- VCS metadata dirs (.git/.svn/.hg/.bzr/.jj/.sl) are excluded automatically.
- Long match lines are capped (``--max-columns 500``) to avoid base64/minified
  noise flooding the result.
- Results default to the first 250 entries (``head_limit``) to protect context;
  pass ``head_limit=0`` for unlimited. ``offset`` paginates.
- ``files_with_matches`` results are sorted by mtime (most recent first), and all
  paths are relativized to the working directory to save tokens.

Differences from Claude Code's tool, by design:
- CC's ``-A/-B/-C/-n/-i`` flag names aren't valid Python identifiers, and this
  framework derives the LLM schema from the ``call()`` signature, so they are
  spelled ``after_context/before_context/context/line_numbers/case_insensitive``
  (the docstring notes the rg equivalents).
- The pure-Python fallback does not honor .gitignore (ripgrep does); it only
  applies the VCS/dir exclusions and glob/type filters described above.
- No ripgrep permission/ignore-pattern integration (this framework has no
  per-Role file-read ignore list to consult).
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
from typing import ClassVar, Optional

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.executor.dependency._document import (
    DOCUMENT_EXTENSIONS as _DOCUMENT_EXTENSIONS,
    extract_document_text as _extract_document_text,
    is_document as _is_document,
)
from metagpt.common.const.tools import (
    VCS_DIRECTORIES_TO_EXCLUDE,
    DEFAULT_HEAD_LIMIT,
    MAX_COLUMNS,
    SEARCH_TIMEOUT,
)
from metagpt.common.prompt.tools import GREP_DESCRIPTION

# Minimal `rg --type` name -> file extension map for the Python fallback.
# ripgrep itself knows hundreds of types; this covers the common ones the
# prompt advertises (js, py, rust, go, java, ...).
_TYPE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "js": (".js", ".jsx", ".mjs", ".cjs"),
    "ts": (".ts", ".tsx", ".mts", ".cts"),
    "py": (".py", ".pyi"),
    "rust": (".rs",),
    "go": (".go",),
    "java": (".java",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"),
    "cs": (".cs",),
    "rb": (".rb",),
    "php": (".php",),
    "sh": (".sh", ".bash", ".zsh"),
    "html": (".html", ".htm"),
    "css": (".css", ".scss", ".sass", ".less"),
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "md": (".md", ".markdown"),
    "toml": (".toml",),
    "xml": (".xml",),
    "csv": (".csv",),
    # Rich document types. CSV above is plain text; these need extraction.
    "pdf": (".pdf",),
    "docx": (".docx",),
    "word": (".docx",),
    "xlsx": (".xlsx",),
    "excel": (".xlsx",),
}

# Rich document extension/extraction handling lives in the shared _document
# module so the Grep and Read tools agree on text and line numbering. CSV is
# intentionally not a document — it is plain text, searched directly.

# `type` values that name a rich document format. ripgrep has no built-in type
# for these, so when one is requested we skip the ripgrep text pass entirely and
# rely on the document-extraction pass (which filters by the same type).
_DOC_ONLY_TYPES = frozenset({"pdf", "docx", "word", "xlsx", "excel"})


def _find_ripgrep() -> Optional[str]:
    """Locate a usable ripgrep binary, or None to use the Python fallback.

    Checks PATH first, then a few well-known vendored locations. A shell alias
    (e.g. `alias rg=...`) is NOT a real binary, so shutil.which may miss it; the
    explicit-path probe covers the vendored-by-another-tool case.
    """
    found = shutil.which("rg")
    if found:
        return found
    candidates = [
        "/usr/bin/rg",
        "/usr/local/bin/rg",
        os.path.expanduser("~/.cargo/bin/rg"),
        "/usr/lib/node_modules/@anthropic-ai/claude-code/vendor/ripgrep/x64-linux/rg",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _split_glob(glob: str) -> list[str]:
    """Split a glob argument on whitespace/commas, preserving brace groups.

    Mirrors CC: "*.{ts,tsx}" stays intact, while "*.js,*.ts" or "*.js *.ts"
    become two patterns.
    """
    patterns: list[str] = []
    for raw in glob.split():
        if "{" in raw and "}" in raw:
            patterns.append(raw)
        else:
            patterns.extend(p for p in raw.split(",") if p)
    return patterns


def _apply_head_limit(items: list, limit: Optional[int], offset: int) -> tuple[list, Optional[int]]:
    """Slice items by offset/limit. Returns (sliced, applied_limit).

    applied_limit is only set when truncation actually happened, so callers know
    there may be more results to paginate. limit=0 means unlimited.
    """
    if limit == 0:
        return items[offset:], None
    effective = DEFAULT_HEAD_LIMIT if limit is None else limit
    sliced = items[offset:offset + effective]
    truncated = (len(items) - offset) > effective
    return sliced, (effective if truncated else None)


@register_tool
class Grep(BaseTool):
    """A powerful content search tool built on ripgrep (with a Python fallback)."""

    name = "Grep"
    aliases: ClassVar[list[str]] = ["Grep.run", "grep", "search"]
    # Grep output is usually compact; cap below the default (CC).
    max_result_size_chars: ClassVar[int] = 20_000
    description = GREP_DESCRIPTION

    async def call(
        self,
        *,
        pattern: str,
        path: str = "",
        glob: str = "",
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
    ) -> str:
        """Search file contents with a regular expression.

        Supports full regex syntax (e.g. "log.*Error", "function\\s+\\w+").
        Searches plain-text files (including .csv) directly, and also looks
        inside rich documents — PDF (.pdf), Word (.docx) and Excel (.xlsx) — by
        extracting their text first (each PDF page / Word paragraph / Excel row
        becomes a searchable line). Version-control metadata directories are
        excluded automatically and results default to the first 250 entries
        (pass head_limit=0 for unlimited, use offset to paginate).

        Args:
            pattern: The regular expression to search for in file contents.
            path: File or directory to search in. Defaults to the current
                working directory.
            glob: Glob pattern to filter files, e.g. "*.py", "*.pdf" or
                "*.{ts,tsx}". Multiple patterns may be comma- or
                space-separated.
            type: File type to restrict the search to, e.g. "py", "js", "rust",
                "go", "java", or a document type "pdf", "docx"/"word",
                "xlsx"/"excel", "csv". More efficient than glob for standard
                types.
            output_mode: One of "files_with_matches" (default; one line per
                matching file as "path:line", where line is the first match's
                line number — pass it to Read's offset to jump straight there),
                "content" (matching lines as "path:line:text"), or "count"
                (per-file match counts).
            case_insensitive: Case-insensitive search (ripgrep -i).
            line_numbers: Show line numbers in content mode (ripgrep -n).
                Defaults to True. Ignored unless output_mode="content".
            before_context: Lines of context to show before each match (rg -B).
                Only used when output_mode="content".
            after_context: Lines of context to show after each match (rg -A).
                Only used when output_mode="content".
            context: Lines of context to show before AND after each match
                (rg -C); takes precedence over before/after. Content mode only.
            multiline: Allow patterns to span lines, where "." also matches
                newlines (ripgrep -U --multiline-dotall). Default False.
            head_limit: Limit output to the first N entries (like "| head -N").
                Defaults to 250; pass 0 for unlimited (use sparingly).
            offset: Skip the first N entries before applying head_limit, for
                pagination. Default 0.
        """
        if not pattern or not pattern.strip():
            raise ToolError("Error: 'pattern' argument is required.")
        if output_mode not in ("files_with_matches", "content", "count"):
            raise ToolError(
                f"Error: invalid output_mode '{output_mode}'. Must be one of "
                f"'files_with_matches', 'content', 'count'."
            )

        search_root = os.path.abspath(os.path.expanduser(path.strip())) if path.strip() else os.getcwd()
        if not os.path.exists(search_root):
            raise ToolError(
                f"Error: path does not exist: {path}. The path should be "
                f"absolute or relative to the current working directory "
                f"({os.getcwd()})."
            )

        rg = _find_ripgrep()
        # A doc-only type (pdf/docx/xlsx/...) has no ripgrep --type, and matches
        # only rich documents, so skip the ripgrep text pass entirely for it.
        doc_only = type in _DOC_ONLY_TYPES
        try:
            if rg is not None and not doc_only:
                rows = await self._run_ripgrep(rg, search_root, pattern, glob, type,
                                               output_mode, case_insensitive, line_numbers,
                                               before_context, after_context, context, multiline)
                # ripgrep can't read PDF/Word/Excel (binary or zipped XML), so it
                # silently skips them. Run a separate extraction pass and merge —
                # no overlap, since rg never matched those files.
                rows += self._run_documents(search_root, pattern, glob, type, output_mode,
                                            case_insensitive, line_numbers, multiline)
            else:
                # Python engine: handles plain text AND documents in one pass.
                rows = self._run_python(search_root, pattern, glob, type, output_mode,
                                        case_insensitive, line_numbers, multiline)
        except TimeoutError:
            raise ToolError(
                f"Error: search timed out after {SEARCH_TIMEOUT:.0f}s. Try a more "
                f"specific path or pattern."
            )
        except re.error as e:
            raise ToolError(f"Error: invalid regular expression '{pattern}': {e}")
        except Exception as e:  # noqa: BLE001 — surface the failure to the model
            raise ToolError(f"Error running search: {e}")

        return self._format(rows, search_root, output_mode, head_limit, offset)

    async def _run_ripgrep(self, rg, root, pattern, glob, type_, output_mode,
                           case_insensitive, line_numbers, before_context,
                           after_context, context, multiline) -> list[str]:
        """Run the ripgrep binary and return its stdout lines (no trailing CR).

        Exit code 0 = matches, 1 = no matches (both fine); anything else raises.
        """
        args = [rg, "--hidden", "--max-columns", str(MAX_COLUMNS)]
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
        for gp in _split_glob(glob):
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

    def _run_python(self, root, pattern, glob, type_, output_mode,
                    case_insensitive, line_numbers, multiline) -> list[str]:
        """Pure-Python fallback search, emitting ripgrep-shaped output lines.

        files_with_matches -> "<path>"; count -> "<path>:<n>";
        content -> "<path>:<lineno>:<text>" (or "<path>:<text>" without numbers).
        Honors the VCS/dir exclusions, glob and type filters, but NOT .gitignore.
        """
        flags = re.MULTILINE
        if case_insensitive:
            flags |= re.IGNORECASE
        if multiline:
            flags |= re.DOTALL
        regex = re.compile(pattern, flags)

        globs = _split_glob(glob)
        type_exts = _TYPE_EXTENSIONS.get(type_, ()) if type_ else ()
        rows: list[str] = []

        for file_path in self._walk_files(root):
            if not self._file_matches_filters(file_path, root, globs, type_exts):
                continue
            if _is_document(file_path):
                continue  # handled by the document-extraction pass below
            try:
                with open(file_path, "r", encoding="utf-8", errors="strict") as f:
                    data = f.read()
            except (OSError, UnicodeDecodeError):
                continue  # skip unreadable / binary (non-utf8) files, like rg

            if multiline:
                if regex.search(data):
                    self._collect(rows, output_mode, file_path, data, regex,
                                  line_numbers, multiline=True)
            else:
                if regex.search(data):
                    self._collect(rows, output_mode, file_path, data, regex,
                                  line_numbers, multiline=False)
        rows += self._run_documents(root, pattern, glob, type_, output_mode,
                                    case_insensitive, line_numbers, multiline)
        return rows

    def _run_documents(self, root, pattern, glob, type_, output_mode,
                       case_insensitive, line_numbers, multiline) -> list[str]:
        """Search rich documents (PDF/Word/Excel) by extracting their text first.

        Each document's extracted text is matched with the same regex and fed
        through _collect, so output is ripgrep-shaped and the existing
        sort/limit/format logic applies unchanged. Documents whose format has no
        available extractor (missing optional dep) are silently skipped, exactly
        like ripgrep skips binaries.
        """
        flags = re.MULTILINE
        if case_insensitive:
            flags |= re.IGNORECASE
        if multiline:
            flags |= re.DOTALL
        regex = re.compile(pattern, flags)

        globs = _split_glob(glob)
        type_exts = _TYPE_EXTENSIONS.get(type_, ()) if type_ else ()
        rows: list[str] = []

        for file_path in self._walk_files(root):
            if not _is_document(file_path):
                continue
            if not self._file_matches_filters(file_path, root, globs, type_exts):
                continue
            data = _extract_document_text(file_path)
            if not data:
                continue
            if regex.search(data):
                self._collect(rows, output_mode, file_path, data, regex,
                              line_numbers, multiline=multiline)
        return rows

    @staticmethod
    def _walk_files(root: str):
        """Yield file paths under root, pruning VCS metadata directories."""
        if os.path.isfile(root):
            yield root
            return
        excluded = set(VCS_DIRECTORIES_TO_EXCLUDE)
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
            # emit the matched spans, truncated to MAX_COLUMNS.
            for m in regex.finditer(data):
                snippet = m.group(0).replace("\n", "\\n")
                if len(snippet) > MAX_COLUMNS:
                    snippet = snippet[:MAX_COLUMNS]
                rows.append(f"{file_path}:{snippet}")
            return
        for i, line in enumerate(data.splitlines(), start=1):
            if regex.search(line):
                if len(line) > MAX_COLUMNS:
                    line = line[:MAX_COLUMNS]
                rows.append(f"{file_path}:{i}:{line}" if line_numbers else f"{file_path}:{line}")

    def _format(self, rows, root, output_mode, head_limit, offset) -> str:
        """Render rows into the model-facing result string per output mode."""
        if output_mode == "files_with_matches":
            return self._format_files(rows, root, head_limit, offset)
        if output_mode == "count":
            return self._format_count(rows, root, head_limit, offset)
        return self._format_content(rows, root, head_limit, offset)

    def _format_files(self, rows, root, head_limit, offset) -> str:
        # Rows are "<path>:<lineno>" (lineno = first match, for Read offset).
        # Sort by mtime (most recent first), filename as tiebreaker.
        def parse(row: str) -> tuple[str, str]:
            path, _, lineno = row.rpartition(":")
            return (path, lineno) if path and lineno.isdigit() else (row, "")

        def mtime(path: str) -> float:
            try:
                return os.stat(path).st_mtime
            except OSError:
                return 0.0

        parsed = [parse(r) for r in rows]
        ordered = sorted(parsed, key=lambda pl: (-mtime(pl[0]), pl[0]))
        limited, applied = _apply_head_limit(ordered, head_limit, offset)
        if not limited:
            return "No files found"
        lines = [
            f"{self._rel(path, root)}:{lineno}" if lineno else self._rel(path, root)
            for path, lineno in limited
        ]
        header = f"Found {len(lines)} file{'s' if len(lines) != 1 else ''}{self._pagination(applied, offset)}"
        return header + "\n" + "\n".join(lines)

    def _format_count(self, rows, root, head_limit, offset) -> str:
        limited, applied = _apply_head_limit(rows, head_limit, offset)
        total = 0
        files = 0
        out_lines = []
        for line in limited:
            head, _, count_str = line.rpartition(":")
            try:
                count = int(count_str)
            except ValueError:
                out_lines.append(line)
                continue
            total += count
            files += 1
            out_lines.append(f"{self._rel(head, root)}:{count}")
        body = "\n".join(out_lines) if out_lines else "No matches found"
        summary = (
            f"\n\nFound {total} total "
            f"{'occurrence' if total == 1 else 'occurrences'} across "
            f"{files} {'file' if files == 1 else 'files'}"
            f"{self._pagination(applied, offset)}"
        )
        return body + summary

    def _format_content(self, rows, root, head_limit, offset) -> str:
        limited, applied = _apply_head_limit(rows, head_limit, offset)
        if not limited:
            return "No matches found"
        out = []
        for line in limited:
            # Lines are "<abs path>:<rest>"; relativize the path prefix only.
            idx = line.find(":")
            if idx > 0:
                out.append(self._rel(line[:idx], root) + line[idx:])
            else:
                out.append(line)
        body = "\n".join(out)
        pag = self._pagination(applied, offset)
        return body + (f"\n\n[Showing results with pagination ={pag.lstrip(' ')}]" if pag else "")

    @staticmethod
    def _rel(abs_path: str, root: str) -> str:
        """Relativize a path against cwd to save tokens; fall back to abs path."""
        try:
            return os.path.relpath(abs_path, os.getcwd())
        except ValueError:
            return abs_path

    @staticmethod
    def _pagination(applied_limit, offset) -> str:
        parts = []
        if applied_limit is not None:
            parts.append(f"limit: {applied_limit}")
        if offset:
            parts.append(f"offset: {offset}")
        return f" ({', '.join(parts)})" if parts else ""
