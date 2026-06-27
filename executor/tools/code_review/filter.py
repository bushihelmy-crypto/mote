"""Deterministic file filter for the code-review pipeline.

Decides which parsed :class:`FileDiff` entries are worth sending to the agent
reviewer. Skips binary blobs, deleted files, unsupported extensions, and a
default set of excluded paths (tests, vendored deps, lockfiles, generated
output). Mirrors OCR's pre-review filtering — the agent should only spend a
tool-loop on source files that actually changed.
"""
from __future__ import annotations

import fnmatch
import os
from typing import Iterable, Optional

from .parser import FileDiff

# Source extensions worth reviewing. MVP white-list — extend as needed.
DEFAULT_SUPPORTED_EXTS: frozenset[str] = frozenset(
    {
        ".py", ".pyi",
        ".go",
        ".js", ".jsx", ".mjs", ".cjs",
        ".ts", ".tsx",
        ".java", ".kt", ".scala",
        ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh",
        ".cs",
        ".rb",
        ".rs",
        ".php",
        ".swift",
        ".m", ".mm",
        ".sh", ".bash",
        ".sql",
        ".vue", ".svelte",
    }
)

# glob patterns (matched against the new-side path) excluded by default.
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "*_test.go",
    "*_test.py",
    "test_*.py",
    "*.test.js",
    "*.test.ts",
    "*.spec.js",
    "*.spec.ts",
    "*/tests/*",
    "*/test/*",
    "*/ztest/*",
    "*/__tests__/*",
    "*/node_modules/*",
    "*/vendor/*",
    "*/dist/*",
    "*/build/*",
    "*/.venv/*",
    "*/migrations/*",
    "*.min.js",
    "*.min.css",
    "*.lock",
    "*.sum",
    "package-lock.json",
    "yarn.lock",
    "*.pb.go",
    "*_pb2.py",
    "*.generated.*",
)


def _matches_any(path: str, globs: Iterable[str]) -> bool:
    """True when *path* (or its basename) matches any glob in *globs*.

    A ``*/dir/*`` pattern is also tested against the path with a synthetic
    leading slash so it matches a root-level ``dir/...`` (fnmatch's ``*`` does
    not span a path that begins with the dir, since there's no leading segment).
    """
    base = os.path.basename(path)
    rooted = "/" + path
    for pat in globs:
        if (
            fnmatch.fnmatch(path, pat)
            or fnmatch.fnmatch(base, pat)
            or fnmatch.fnmatch(rooted, pat)
        ):
            return True
    return False


def should_review(
    file_diff: FileDiff,
    *,
    supported_exts: Optional[Iterable[str]] = None,
    exclude_globs: Optional[Iterable[str]] = None,
) -> bool:
    """Return True if *file_diff* should be sent to the agent reviewer.

    Args:
        file_diff: A parsed file diff.
        supported_exts: Override the extension white-list (defaults to
            :data:`DEFAULT_SUPPORTED_EXTS`). Extensions include the dot.
        exclude_globs: Override the default exclude patterns.

    Skips: binary files, deleted files, files with no reviewable hunks,
    unsupported extensions, and default-excluded paths.
    """
    exts = frozenset(supported_exts) if supported_exts is not None else DEFAULT_SUPPORTED_EXTS
    globs = tuple(exclude_globs) if exclude_globs is not None else DEFAULT_EXCLUDE_GLOBS

    if file_diff.is_binary or file_diff.is_deleted:
        return False
    if not file_diff.hunks:
        return False

    _root, ext = os.path.splitext(file_diff.path)
    if ext.lower() not in exts:
        return False

    if _matches_any(file_diff.path, globs):
        return False

    return True
