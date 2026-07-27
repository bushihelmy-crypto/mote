"""Legacy rollout decoding isolated from the current session runtime."""

from __future__ import annotations

import json
from typing import Any, Optional

from mote.runtime.events.journal import decode_event_record
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import SESSION_EVENT_CLASSES, SessionEvent, SessionMetaEvent


def parse_legacy_record(line: str) -> Optional[dict[str, Any]]:
    """Forgiving decoder for historic ``{type, ts, payload}`` records."""

    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or "type" not in record:
        return None
    return record


def decode_legacy_event(record: dict[str, Any]) -> Optional[SessionEvent]:
    """Reconstruct a known historic payload without entering live code paths."""

    event_class = SESSION_EVENT_CLASSES.get(record.get("type") or "")
    if event_class is None:
        return None
    try:
        return event_class.from_payload(record.get("payload") or {})
    except Exception:
        return None


def decode_session_meta_record(raw: bytes) -> SessionMetaEvent | None:
    """Decode first-line metadata from either a v3 or historic rollout."""

    try:
        envelope = decode_event_record(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        try:
            record = parse_legacy_record(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return None
        event = decode_legacy_event(record) if record is not None else None
    else:
        if envelope.sequence != 1:
            return None
        event = decode_session_event(envelope)
    return event if isinstance(event, SessionMetaEvent) else None


__all__ = [
    "decode_legacy_event",
    "decode_session_meta_record",
    "parse_legacy_record",
]
