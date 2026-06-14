"""Glob tool — aligned with Claude Code's Glob (GlobTool, built on ripgrep).

Fast file-name pattern matching that works on any codebase size. Prefers the
ripgrep binary (`rg --files --glob`) when available and falls back to Python's
`glob` module otherwise, so it works even where ripgrep isn't installed. The
interface and defaults mirror Claude Code's tool so model behavior stays
familiar:

- Two parameters: ``pattern`` (required, e.g. "**/*.js") and ``path`` (optional
  directory to search; defaults to the current working directory).
- Matching paths are returned sorted by modification time, most recent first.
- Hidden files are included; version-control metadata dirs (.git/.svn/...) are
  excluded.
- Results are capped at the first 100 files to protect context; when truncated,
  a note suggests narrowing the pattern or path. Paths are relativized to the
  working directory to save tokens.

Differences from Claude Code's tool, by design:
- The Python fallback does not honor .gitignore (ripgrep, with --no-ignore the
  default in CC, also ignores it); it only applies the VCS/dir exclusions.
- No per-Role file-read ignore-pattern integration (this framework has no such
  list to consult).
"""
from __future__ import annotations

import asyncio
import glob as _glob_mod
import os
from typing import ClassVar, Optional

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.common.const.tools import (
    GLOB_DEFAULT_LIMIT as DEFAULT_LIMIT,
    SEARCH_TIMEOUT,
    VCS_DIRECTORIES_TO_EXCLUDE,
)
from metagpt.executor.tools.grep import _find_ripgrep


@register_tool
class Glob(BaseTool):
    """Fast file pattern matching tool that works with any codebase size."""

    name = "Glob"
    aliases: ClassVar[list[str]] = ["Glob.run", "glob"]
    # Glob can list many paths; allow a higher cap before persisting (CC).
    max_result_size_chars: ClassVar[int] = 100_000
    description = (
        "Fast file pattern matching tool that works with any codebase size. "
        "Supports glob patterns like \"**/*.js\" or \"src/**/*.ts\". Returns "
        "matching file paths sorted by modification time (most recent first). "
        "Use this to find files by name; for content search use Grep instead."
    )

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
            raise ToolError("Error: 'pattern' argument is required.")

        search_root = (
            os.path.abspath(os.path.expanduser(path.strip())) if path.strip() else os.getcwd()
        )
        if path.strip():
            if not os.path.exists(search_root):
                raise ToolError(
                    f"Error: directory does not exist: {path}. The path should "
                    f"be absolute or relative to the current working directory "
                    f"({os.getcwd()})."
                )
            if not os.path.isdir(search_root):
                raise ToolError(f"Error: path is not a directory: {path}")

        rg = _find_ripgrep()
        try:
            if rg is not None:
                files = await self._run_ripgrep(rg, search_root, pattern)
            else:
                files = self._run_python(search_root, pattern)
        except TimeoutError:
            raise ToolError(
                f"Error: search timed out after {SEARCH_TIMEOUT:.0f}s. Try a more "
                f"specific path or pattern."
            )
        except Exception as e:  # noqa: BLE001 — surface the failure to the model
            raise ToolError(f"Error finding files: {e}")

        return self._format(files)

    async def _run_ripgrep(self, rg: str, root: str, pattern: str) -> list[str]:
        """List files under root matching pattern via ripgrep, return abs paths.

        Runs with cwd=root and no path argument (like CC) so ripgrep's --glob
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
    def _format(files: list[str]) -> str:
        """Sort by mtime (most recent first), truncate, relativize, render."""
        def mtime(p: str) -> float:
            try:
                return os.stat(p).st_mtime
            except OSError:
                return 0.0

        ordered = sorted(files, key=lambda p: (-mtime(p), p))
        truncated = len(ordered) > DEFAULT_LIMIT
        limited = ordered[:DEFAULT_LIMIT]
        if not limited:
            return "No files found"

        cwd = os.getcwd()
        rels = []
        for p in limited:
            try:
                rels.append(os.path.relpath(p, cwd))
            except ValueError:
                rels.append(p)
        if truncated:
            rels.append(
                "(Results are truncated. Consider using a more specific path or pattern.)"
            )
        return "\n".join(rels)
