"""Envelope formatting for the unified per-turn ephemeral context block.

All sources' rendered blocks are merged into a single ``<system-reminder>``
envelope (Claude Code's wrapper tag for ephemeral, request-only context). The
bus owns ordering; this module owns the wire shape.
"""

from __future__ import annotations

from typing import Iterable

_OPEN = "<system-reminder>"
_CLOSE = "</system-reminder>"


def wrap_system_reminder(blocks: Iterable[str]) -> str:
    """Join non-empty *blocks* into one ``<system-reminder>`` envelope.

    Blocks are separated by a blank line. Returns ``""`` when nothing is left
    after dropping empty/whitespace-only blocks (so the caller injects nothing).
    """
    kept = [b.strip() for b in blocks if b and b.strip()]
    if not kept:
        return ""
    body = "\n\n".join(kept)
    return f"{_OPEN}\n{body}\n{_CLOSE}"


__all__ = ["wrap_system_reminder"]
