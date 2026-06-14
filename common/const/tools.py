#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool-related constants — file size caps, search limits, document extensions.

Centralized from the individual tool modules so cross-tool behavior is defined
in one place.
"""

# ---------------------------------------------------------------------------
# Document extensions (shared by Grep + Read for consistent line numbering)
# ---------------------------------------------------------------------------
DOCUMENT_EXTENSIONS = (".pdf", ".docx", ".xlsx")

# ---------------------------------------------------------------------------
# File size caps
# ---------------------------------------------------------------------------
MAX_FILE_SIZE_BYTES = 256 * 1024          # Read: text files larger require offset/limit
MAX_MEDIA_SIZE_BYTES = 10 * 1024 * 1024   # Read: hard cap for images/PDFs (10 MB)
MAX_CONTENT_SIZE_BYTES = 10 * 1024 * 1024  # Write: content size cap (10 MB)
MAX_EDIT_FILE_SIZE_BYTES = 10 * 1024 * 1024  # Edit: file size cap (10 MB)
MAX_NOTEBOOK_SIZE_BYTES = 10 * 1024 * 1024   # NotebookEdit: notebook size cap (10 MB)

# ---------------------------------------------------------------------------
# Read tool
# ---------------------------------------------------------------------------
DEFAULT_MAX_LINES = 2000    # default lines returned when limit unspecified
MAX_LINE_LENGTH = 2000      # lines longer than this are truncated
MAX_IMAGE_DIMENSION = 2048  # Read: images whose longest edge exceeds this are
                            # downscaled (detail="high") before being shown to
                            # the model; detail="original" skips the resize.

# ---------------------------------------------------------------------------
# Search / Grep tool
# ---------------------------------------------------------------------------
VCS_DIRECTORIES_TO_EXCLUDE = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")
DEFAULT_HEAD_LIMIT = 250    # default cap on grep results
MAX_COLUMNS = 500           # match lines longer than this are truncated
SEARCH_TIMEOUT = 20.0       # search timeout in seconds

# ---------------------------------------------------------------------------
# Glob tool
# ---------------------------------------------------------------------------
GLOB_DEFAULT_LIMIT = 100    # cap on returned files

# ---------------------------------------------------------------------------
# Error convention
# ---------------------------------------------------------------------------
ERROR_PREFIX = "Error:"
