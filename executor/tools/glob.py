"""Glob tool — file-name pattern matching built on ripgrep.

Fast file-name pattern matching that works on any codebase size. Prefers the
ripgrep binary (`rg --files --glob`) when available and falls back to Python's
`glob` module otherwise, so it works even where ripgrep isn't installed. The
interface and defaults:

- Two parameters: ``pattern`` (required, e.g. "**/*.js") and ``path`` (optional
  directory to search; defaults to the current working directory).
- Matching paths are returned sorted by modification time, most recent first.
- Hidden files are included; version-control metadata dirs (.git/.svn/...) are
  excluded.
- Results are capped at the first 100 files to protect context; when truncated,
  a note suggests narrowing the pattern or path. Paths are relativized to the
  working directory to save tokens.

Differences by design:
- The Python fallback does not honor .gitignore (ripgrep, run with --no-ignore
  by default, also ignores it); it only applies the VCS/dir exclusions.
- No per-Role file-read ignore-pattern integration (this framework has no such
  list to consult).
"""
from __future__ import annotations

import asyncio
import glob as _glob_mod
import os
from typing import ClassVar

from mote.common.const.tools import GLIMPSE_EXTENSIONS, GLIMPSE_RECORD_LIMIT
from mote.common.const.tools import GLOB_DEFAULT_LIMIT as DEFAULT_LIMIT
from mote.common.const.tools import SEARCH_TIMEOUT, VCS_DIRECTORIES_TO_EXCLUDE
from mote.common.prompt.tools import GLOB_DESCRIPTION
from mote.common.text import display_path
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import GetCwd, RecordFileGlimpsed
from mote.executor.dependency._paths import base_cwd, resolve_path
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError
from mote.executor.tools.grep import _find_ripgrep

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise/return site).
_MSG_PATTERN_REQUIRED = "Error: 'pattern' argument is required."
_MSG_DIR_NOT_FOUND = (
    "Error: directory does not exist: {path}. The path should be absolute or "
    "relative to the working directory ({base_cwd})."
)
_MSG_NOT_A_DIRECTORY = "Error: path is not a directory: {path}"
_MSG_SEARCH_TIMEOUT = "Error: search timed out after {seconds:.0f}s. Try a more specific path or pattern."
_MSG_FIND_FAILED = "Error finding files: {error}"
_MSG_NO_FILES = "No files found"
_MSG_TRUNCATED = "(Results are truncated. Consider using a more specific path or pattern.)"


def _mtime(p: str) -> float:
    """File mtime (epoch secs), 0.0 when unstattable — for recency ordering."""
    try:
        return os.stat(p).st_mtime
    except OSError:
        return 0.0


@register_tool
class Glob(BaseTool):
    """Fast file pattern matching tool that works with any codebase size."""

    name = "Glob"
    aliases: ClassVar[list[str]] = ["Glob.run", "glob"]
    # Read-only pattern match: results are re-derivable by re-running the glob.
    reconstructable: ClassVar[bool] = True
    # Glob can list many paths; allow a higher cap before persisting.
    max_result_size_chars: ClassVar[int] = 100_000
    description = GLOB_DESCRIPTION
    # get_cwd is the stable base for the default search root + output
    # relativization. record_file_glimpsed feeds matched files to the code map as
    # navigation hints. Both optional: unbound (no Role) falls back / no-ops.
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
        pattern: str,
        path: str = "",
    ) -> str:
        """Find files whose paths match a glob pattern.

        Supports glob patterns like "**/*.js" or "src/**/*.ts". Returns the
        matching file paths sorted by modification time (most recent first),
        relativized to the working directory. Hidden files are included;
        version-control metadata directories are excluded. Results are limited
        to the first 100 files.

        Args:
            pattern: The glob pattern to match files against, e.g. "**/*.py".
            path: The directory to search in. If not specified, the current
                working directory is used. IMPORTANT: omit this field to use
                the default directory; do NOT pass "undefined" or "null". Must
                be a valid directory if provided.
        """
        if not pattern or not pattern.strip():
            raise ToolError(_MSG_PATTERN_REQUIRED)

        base_cwd = self._base_cwd()
        search_root = resolve_path(getattr(self, "get_cwd", None), path.strip()) if path.strip() else base_cwd
        if path.strip():
            if not os.path.exists(search_root):
                raise ToolError(_MSG_DIR_NOT_FOUND.format(path=path, base_cwd=base_cwd))
            if not os.path.isdir(search_root):
                raise ToolError(_MSG_NOT_A_DIRECTORY.format(path=path))

        rg = _find_ripgrep()
        try:
            if rg is not None:
                files = await self._run_ripgrep(rg, search_root, pattern)
            else:
                files = self._run_python(search_root, pattern)
        except TimeoutError:
            raise ToolError(_MSG_SEARCH_TIMEOUT.format(seconds=SEARCH_TIMEOUT))
        except Exception as e:  # noqa: BLE001 — surface the failure to the model
            raise ToolError(_MSG_FIND_FAILED.format(error=e))

        self._record_glimpses(files)
        return self._format(files, base_cwd)

    def _record_glimpses(self, files: list[str]) -> None:
        """Feed the top matched ``.py`` files to the code map as glimpse hints (P2).

        Records at most :data:`GLIMPSE_RECORD_LIMIT` files, most-recently-modified
        first (the same ordering :meth:`_format` surfaces), so the map can show
        their structure to guide "which of these should I open". Unbound (no
        Role) → the capability is absent → no-op. Best-effort: a raising sink is
        swallowed (a navigation hint must never fail a search).
        """
        record = getattr(self, "record_file_glimpsed", None)
        if record is None:
            return
        py = [p for p in files if p.endswith(GLIMPSE_EXTENSIONS)]
        ordered = sorted(py, key=lambda p: (-_mtime(p), p))[:GLIMPSE_RECORD_LIMIT]
        for p in ordered:
            try:
                record(os.path.abspath(p))
            except Exception:  # noqa: BLE001 — advisory; never fail the search
                return

    async def _run_ripgrep(self, rg: str, root: str, pattern: str) -> list[str]:
        """List files under root matching pattern via ripgrep, return abs paths.

        Runs with cwd=root and no path argument so ripgrep's --glob
        matching is anchored relative to root; otherwise a path-relative glob
        such as "tools/*.py" would not match.

        Exit code 0 = matches, 1 = no matches (both fine); anything else raises.
        """
        args = [rg, "--files", "--hidden", "--glob", pattern]
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
        text = stdout.decode(errors="replace")
        out = []
        for ln in text.split("\n"):
            ln = ln.rstrip("\r")
            if not ln:
                continue
            out.append(ln if os.path.isabs(ln) else os.path.join(root, ln))
        return out

    @staticmethod
    def _run_python(root: str, pattern: str) -> list[str]:
        """Pure-Python fallback using the glob module (supports ** recursion).

        Honors the VCS metadata exclusions but NOT .gitignore. Hidden files are
        included (include_hidden=True) to match ripgrep's --hidden.

        ripgrep's --glob treats a separator-less pattern (e.g. "*.py") as a
        basename match at ANY depth, whereas glob.glob anchors it to the top
        level. To match ripgrep, such patterns are searched recursively.
        """
        search_pattern = pattern if "/" in pattern else os.path.join("**", pattern)
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

    @staticmethod
    def _format(files: list[str], cwd: str) -> str:
        """Sort by mtime (most recent first), truncate, relativize, render."""
        ordered = sorted(files, key=lambda p: (-_mtime(p), p))
        truncated = len(ordered) > DEFAULT_LIMIT
        limited = ordered[:DEFAULT_LIMIT]
        if not limited:
            return _MSG_NO_FILES

        rels = [display_path(p, cwd) for p in limited]
        if truncated:
            rels.append(_MSG_TRUNCATED)
        return "\n".join(rels)
