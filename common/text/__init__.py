"""Text elision primitives — the single authority for truncation markers."""
from __future__ import annotations

from mote.common.text.ansi import strip_ansi
from mote.common.text.elision import Elision, ElisionStrategy, ElisionUnit, cap_head, cap_head_tail
from mote.common.text.hashing import content_hash
from mote.common.text.humanize import format_elapsed, format_file_size, format_token_count
from mote.common.text.markers import (
    PERSISTED_OUTPUT_CLOSE,
    PERSISTED_OUTPUT_OPEN,
    SYSTEM_REMINDER_CLOSE,
    SYSTEM_REMINDER_OPEN,
    is_system_reminder,
    strip_system_reminder,
    system_reminder,
    wrap_system_reminder,
)
from mote.common.text.paths import display_path, path_to_uri, uri_to_path
from mote.common.text.plural import count_noun, plural, verb_agree
from mote.common.text.whitespace import collapse_whitespace

__all__ = [
    "Elision",
    "ElisionStrategy",
    "ElisionUnit",
    "cap_head",
    "cap_head_tail",
    "strip_ansi",
    "content_hash",
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
