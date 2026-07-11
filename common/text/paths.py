"""Single authority for filesystem-path <-> ``file://`` URI conversion and the
compact relative-path display used across the framework.

Three tiny pure helpers used to live copy-pasted in unrelated leaves that could
not import each other (AGENTS.md layering):

- ``uri_to_path`` / ``path_to_uri`` were duplicated verbatim in
  ``roles/lsp/server.py`` (the LSP transport) and ``context/code_map/__init__.py``
  (the low ``context`` layer, which must NOT import ``roles``). LSP
  ``Location.uri`` values arrive as ``file://`` URIs and get turned back into
  filesystem paths on both sides.
- ``display_path`` (path relativised against a cwd for a compact reminder line)
  lived in ``context/turn_context/sources/_pathfmt.py`` but the same relpath +
  Windows ``ValueError`` fallback was re-derived inline in ``executor/tools``
  (grep ``_rel``, glob).

Homing all three in the bottom ``common`` layer lets every side import one copy.
Zero dependencies beyond the stdlib; no I/O, no provider shapes, no rendering.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse


def path_to_uri(path: str) -> str:
    """Convert an absolute filesystem path to a ``file://`` URI."""
    return Path(os.path.abspath(path)).as_uri()


def uri_to_path(uri: str) -> str:
    """Convert a ``file://`` URI back to a filesystem path (best-effort).

    A non-``file://`` value passes through unchanged so callers can hand any
    string through without pre-checking the scheme.
    """
    if uri.startswith("file://"):
        return unquote(urlparse(uri).path)
    return uri


def display_path(path: str, cwd: Optional[str]) -> str:
    """Render *path* relative to *cwd* (absolute passthrough when that can't be done).

    With no cwd, pass the path through unchanged; with a cwd, relativise it,
    falling back to the absolute path when the two live on different drives (a
    Windows ``os.path.relpath`` ``ValueError``).
    """
    if cwd:
        try:
            return os.path.relpath(path, cwd)
        except ValueError:  # different drive on Windows
            return path
    return path


__all__ = ["path_to_uri", "uri_to_path", "display_path"]
