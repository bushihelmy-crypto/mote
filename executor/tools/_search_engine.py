"""Shared ripgrep engine helpers for the Search tool.

The ripgrep-binary discovery, glob-argument splitting, head/offset pagination,
and the `--type` / document / heavy-directory tables that the unified ``Search``
tool builds on. Kept in one leaf module so the tool file stays focused on
orchestration and result shaping.

(These were extracted from the now-retired ``grep.py`` / ``glob.py`` tools;
``search.py`` is the sole importer and this is their single home.)
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from typing import Optional

# Rich document extension/extraction handling lives in the shared _document
# module so Search and Read agree on text and line numbering. CSV is
# intentionally not a document — it is plain text, searched directly.

# `type` values that name a rich document format. ripgrep has no built-in type
# for these, so when one is requested we skip the ripgrep text pass entirely and
# rely on the document-extraction pass (which filters by the same type).
DOC_ONLY_TYPES = frozenset({"pdf", "docx", "word", "xlsx", "excel"})

# Minimal `rg --type` name -> file extension map for the document pass. ripgrep
# itself knows hundreds of types; this covers the common ones the prompt
# advertises (js, py, rust, go, java, ...) plus the rich-document types.
TYPE_EXTENSIONS: dict[str, tuple[str, ...]] = {
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

# Heavy dependency/build directories the Python passes prune (document pass and
# the ripgrep-absent glob fallback). ripgrep honors .gitignore, so it usually
# skips these; the Python passes do NOT read .gitignore, so without explicit
# pruning they would descend into every node_modules/.venv (potentially
# millions of files), taking effectively forever and blocking the caller.
HEAVY_DIRECTORIES_TO_EXCLUDE = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "site-packages",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        "dist",
        "build",
    }
)

# Our own vendored ripgrep, so we don't depend on a system rg or one shipped by
# another tool. Only x86_64-linux is checked in (see mote/vendor/ripgrep/);
# other platforms fall through to a system rg on PATH.
VENDORED_RIPGREP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "vendor",
    "ripgrep",
    f"{platform.machine()}-{sys.platform}",
    "rg",
)


def find_ripgrep() -> Optional[str]:
    """Locate a usable ripgrep binary, or None if none is available.

    Probe order: system PATH -> our vendored binary -> other well-known
    locations (including one vendored by another globally-installed tool, kept
    only as a last resort). A shell alias (e.g. `alias rg=...`) is NOT a real
    binary, so shutil.which may miss it; the explicit-path probes cover that.
    """
    found = shutil.which("rg")
    if found:
        return found
    candidates = [
        VENDORED_RIPGREP,
        "/usr/bin/rg",
        "/usr/local/bin/rg",
        os.path.expanduser("~/.cargo/bin/rg"),
        # Last resort: a ripgrep vendored by another globally-installed tool.
        "/usr/lib/node_modules/@anthropic-ai/claude-code/vendor/ripgrep/x64-linux/rg",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def split_glob(glob: str) -> list[str]:
    """Split a glob argument on whitespace/commas, preserving brace groups.

    "*.{ts,tsx}" stays intact, while "*.js,*.ts" or "*.js *.ts" become two
    patterns.
    """
    patterns: list[str] = []
    for raw in glob.split():
        if "{" in raw and "}" in raw:
            patterns.append(raw)
        else:
            patterns.extend(p for p in raw.split(",") if p)
    return patterns


def apply_head_limit(items: list, limit: Optional[int], offset: int) -> tuple[list, Optional[int]]:
    """Slice items by offset/limit. Returns (sliced, applied_limit).

    applied_limit is only set when truncation actually happened, so callers know
    there may be more results to paginate. limit is unbounded unless the caller
    asks for a specific N (limit=0 or None both mean unlimited); a large result
    is persisted to disk by the shared tool-result exit rather than capped here.
    """
    if not limit:
        return items[offset:], None
    sliced = items[offset : offset + limit]
    truncated = (len(items) - offset) > limit
    return sliced, (limit if truncated else None)
