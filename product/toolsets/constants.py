#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Built-in Toolset limits and search policy."""

import sys


def _is_wsl() -> bool:
    """True when running under WSL (Windows Subsystem for Linux).

    WSL's 9p-backed filesystem access is markedly slower than a native Linux
    disk, so searches over Windows-mounted trees need a longer budget. Detected
    by looking for the "microsoft"/"wsl" marker the WSL kernel exposes in
    ``/proc/sys/kernel/osrelease``.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8") as f:
            release = f.read().lower()
    except OSError:
        return False
    return "microsoft" in release or "wsl" in release


# ---------------------------------------------------------------------------
# File size caps
# ---------------------------------------------------------------------------
MAX_FILE_SIZE_BYTES = 256 * 1024  # Read: text files larger require offset/limit
MAX_MEDIA_SIZE_BYTES = 10 * 1024 * 1024  # Read: hard cap for images/PDFs (10 MB)
MAX_CONTENT_SIZE_BYTES = 10 * 1024 * 1024  # Write: content size cap (10 MB)
MAX_EDIT_FILE_SIZE_BYTES = 10 * 1024 * 1024  # Edit: file size cap (10 MB)

# ---------------------------------------------------------------------------
# Read tool
# ---------------------------------------------------------------------------
MAX_IMAGE_DIMENSION = 2048  # Read: images whose longest edge exceeds this are
# downscaled (detail="high") before being shown to
# the model; detail="original" skips the resize.

# ---------------------------------------------------------------------------
# Search / Grep tool
# ---------------------------------------------------------------------------
VCS_DIRECTORIES_TO_EXCLUDE = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")
# Search budget: WSL's 9p filesystem is much slower over Windows-mounted trees,
# so it gets a longer deadline (60s on WSL, 20s default).
SEARCH_TIMEOUT = 60.0 if _is_wsl() else 20.0  # search timeout in seconds

# ---------------------------------------------------------------------------
# Code-map glimpse (Search → code map navigation hint)
# ---------------------------------------------------------------------------
# Cap on how many matched files a single Search call records as "glimpsed"
# for the code map. A search can match hundreds of files; recording them all
# would flood the map (and its per-turn parse). Only the top-N (result order —
# mtime-sorted, so the most recently touched) become navigation hints.
GLIMPSE_RECORD_LIMIT = 20
# Keep this aligned with the providers in runtime.code_map._langconfigs.
# Search stays lightweight and does not initialize the native parser runtime.
GLIMPSE_EXTENSIONS = (
    ".c",
    ".cc",
    ".cjs",
    ".cpp",
    ".cs",
    ".cxx",
    ".go",
    ".h",
    ".hh",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".mjs",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
)
