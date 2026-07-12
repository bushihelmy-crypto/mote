"""apply_patch dependency package — a Python port of codex's ``apply-patch`` crate.

Splits the structured multi-file patch capability into three pure, IO-free
modules so the :class:`~metagpt.executor.tools.apply_patch.ApplyPatch` tool can
compose them with this fork's permission engine and read-before-write guard:

- :mod:`parser` — markers, hunk dataclasses, and the ``parse_patch`` state
  machine (port of ``parser.rs`` + ``streaming_parser.rs``).
- :mod:`seek` — the 5-pass fuzzy line matcher (port of ``seek_sequence.rs``).
- :mod:`applier` — ``apply_update`` / ``affected_paths`` (port of the ``lib.rs``
  compute/apply region).

These mirror codex's freeform self-parsing: the model emits the patch text, our
parser does the structured interpretation. Nothing here is a registered tool, so
it lives outside the registry's package scan of ``tools/``.
"""
from __future__ import annotations

from metagpt.executor.dependency._apply_patch.applier import (
    affected_paths,
    apply_update,
)
from metagpt.executor.dependency._apply_patch.parser import (
    AddFile,
    ApplyPatchError,
    DeleteFile,
    Hunk,
    UpdateFile,
    UpdateFileChunk,
    hunk_path,
    parse_patch,
)
from metagpt.executor.dependency._apply_patch.seek import seek_sequence

__all__ = [
    "parse_patch",
    "Hunk",
    "AddFile",
    "DeleteFile",
    "UpdateFile",
    "UpdateFileChunk",
    "hunk_path",
    "apply_update",
    "affected_paths",
    "seek_sequence",
    "ApplyPatchError",
]
