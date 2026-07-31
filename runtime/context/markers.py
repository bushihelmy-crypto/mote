"""Single authority for model-facing context envelope markers.

The framework wraps ephemeral / persisted content in a handful of angle-bracket
sentinel tags the model reads (and the CLI later peels back off). This module
homes the tag *literals* once in the bottom ``common`` layer, so the write side
(``context/turn_context/format.py``), the read side (``cli/view/reminders.py``),
and ``executor/tools/read.py`` cannot silently desync.

Zero dependencies beyond the stdlib; no I/O, no provider shapes, no rendering.
"""
from __future__ import annotations

from typing import Iterable

from mote.contracts.tool.output_markers import PERSISTED_OUTPUT_CLOSE, PERSISTED_OUTPUT_OPEN

# --- <system-reminder>: ephemeral, request-only per-turn context envelope -----
# The wrapper for content the model sees each turn but that is NOT
# stored in history. The turn-context bus writes it; the CLI projector detects &
# peels it to fold injected context apart from the human's own typed prompt.
SYSTEM_REMINDER_OPEN = "<system-reminder>"
SYSTEM_REMINDER_CLOSE = "</system-reminder>"


# --- <persisted-output>: over-large tool result spilled to disk ---------------
# ``tool_result_limit`` replaces an inline result too big to send with this
# envelope wrapping a short preview + the on-disk path. Re-exported from here so
# both the executor (write) and the CLI projector (read) share one literal.
def wrap_system_reminder(blocks: Iterable[str]) -> str:
    """Join non-empty *blocks* into one ``<system-reminder>`` envelope.

    Blocks are separated by a blank line. Returns ``""`` when nothing is left
    after dropping empty/whitespace-only blocks (so the caller injects nothing).
    """
    kept = [b.strip() for b in blocks if b and b.strip()]
    if not kept:
        return ""
    body = "\n\n".join(kept)
    return f"{SYSTEM_REMINDER_OPEN}\n{body}\n{SYSTEM_REMINDER_CLOSE}"


def is_system_reminder(content: str) -> bool:
    """True iff *content* is exactly a ``<system-reminder>`` envelope.

    Strict on both ends so a human prompt that merely mentions the tag in prose
    is never mistaken for injected context — only the bus's own wrapper (opens
    with the tag and closes with it) qualifies.
    """
    stripped = content.strip()
    return stripped.startswith(SYSTEM_REMINDER_OPEN) and stripped.endswith(SYSTEM_REMINDER_CLOSE)


def strip_system_reminder(content: str) -> str:
    """Peel the ``<system-reminder>`` tags off, returning the inner block text."""
    inner = content.strip()
    if inner.startswith(SYSTEM_REMINDER_OPEN):
        inner = inner[len(SYSTEM_REMINDER_OPEN) :]
    if inner.endswith(SYSTEM_REMINDER_CLOSE):
        inner = inner[: -len(SYSTEM_REMINDER_CLOSE)]
    return inner.strip()


def system_reminder(text: str) -> str:
    """Wrap a single already-formatted *text* body in one envelope (no block join).

    For one-shot inline notices (e.g. a file-read warning) that aren't part of the
    multi-block turn-context merge.
    """
    return f"{SYSTEM_REMINDER_OPEN}{text}{SYSTEM_REMINDER_CLOSE}"


__all__ = [
    "SYSTEM_REMINDER_OPEN",
    "SYSTEM_REMINDER_CLOSE",
    "PERSISTED_OUTPUT_OPEN",
    "PERSISTED_OUTPUT_CLOSE",
    "wrap_system_reminder",
    "is_system_reminder",
    "strip_system_reminder",
    "system_reminder",
]
