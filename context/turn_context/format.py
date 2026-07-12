"""Envelope formatting for the unified per-turn ephemeral context block.

All sources' rendered blocks are merged into a single ``<system-reminder>``
envelope (the wrapper tag for ephemeral, request-only context). The
bus owns ordering; this module owns the wire shape.
"""

from __future__ import annotations

# The envelope shape is owned by the bottom-layer marker authority
# (``common/text/markers.py``); the bus only decides ordering. Re-exported here
# so the historical import path ``mote.context.turn_context.wrap_system_reminder``
# keeps working.
from mote.common.text import wrap_system_reminder

__all__ = ["wrap_system_reminder"]
