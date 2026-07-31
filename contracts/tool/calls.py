"""Stable tool-call wire helpers."""

from __future__ import annotations

import json
from typing import Any


def serialize_tool_call_args(args: Any) -> str:
    """Serialize tool arguments to the canonical wire JSON string.

    Strings pass through unchanged because persisted-argument envelopes are
    already serialized.  Other values use compact semantic defaults shared by
    message projection, large-argument persistence, and compaction spilling.
    """

    return args if isinstance(args, str) else json.dumps(args or {})


__all__ = ["serialize_tool_call_args"]
