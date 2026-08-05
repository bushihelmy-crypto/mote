"""Stable tool-call wire helpers."""

from __future__ import annotations

import json

from mote.contracts.events.envelope import freeze_json, thaw_json


def serialize_tool_call_args(args: object) -> str:
    """Serialize tool arguments to the canonical wire JSON string.

    Strings pass through unchanged because persisted-argument envelopes are
    already serialized.  Other values use compact semantic defaults shared by
    message projection, large-argument persistence, and compaction spilling.
    """

    if isinstance(args, str):
        return args
    frozen = freeze_json(args or {}, path="tool call arguments")
    return json.dumps(thaw_json(frozen))


__all__ = ["serialize_tool_call_args"]
