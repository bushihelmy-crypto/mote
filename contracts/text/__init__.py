"""Text elision primitives — the single authority for truncation markers."""
from __future__ import annotations

from mote.contracts.text.ansi import strip_ansi
from mote.contracts.text.elision import Elision, ElisionStrategy, ElisionUnit, cap_head, cap_head_tail
from mote.contracts.text.hashing import content_hash
from mote.contracts.text.html import html_to_markdown
from mote.contracts.text.humanize import format_elapsed, format_file_size, format_token_count
from mote.contracts.text.hunks import (
    MAX_DIFF_SIZE_BYTES,
    Hunk,
    HunkApplyError,
    apply_hunk,
    apply_hunks,
    patch_lines,
    revert_hunk,
    revert_hunks,
    slice_lines,
    split_hunks,
)
from mote.contracts.text.markers import (
    PERSISTED_OUTPUT_CLOSE,
    PERSISTED_OUTPUT_OPEN,
    SYSTEM_REMINDER_CLOSE,
    SYSTEM_REMINDER_OPEN,
    is_system_reminder,
    strip_system_reminder,
    system_reminder,
    wrap_system_reminder,
)
from mote.contracts.text.paths import display_path, path_to_uri, uri_to_path
from mote.contracts.text.plural import count_noun, plural, verb_agree
from mote.contracts.text.whitespace import collapse_whitespace

__all__ = [
    "Elision",
    "ElisionStrategy",
    "ElisionUnit",
    "cap_head",
    "cap_head_tail",
    "strip_ansi",
    "content_hash",
    "html_to_markdown",
    "Hunk",
    "HunkApplyError",
    "MAX_DIFF_SIZE_BYTES",
    "patch_lines",
    "slice_lines",
    "split_hunks",
    "apply_hunk",
    "revert_hunk",
    "apply_hunks",
    "revert_hunks",
    "count_noun",
    "plural",
    "verb_agree",
    "collapse_whitespace",
    "format_elapsed",
    "format_file_size",
    "format_token_count",
    "display_path",
    "path_to_uri",
    "uri_to_path",
    "PERSISTED_OUTPUT_CLOSE",
    "PERSISTED_OUTPUT_OPEN",
    "SYSTEM_REMINDER_CLOSE",
    "SYSTEM_REMINDER_OPEN",
    "is_system_reminder",
    "strip_system_reminder",
    "system_reminder",
    "wrap_system_reminder",
]
