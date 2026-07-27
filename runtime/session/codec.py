"""Stable session fact names and typed payload encoding for journal envelopes."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Optional, cast
from uuid import uuid4

from mote.contracts.events import EventEnvelope, EventId, EventType, JsonValue
from mote.contracts.events.envelope import thaw_json
from mote.contracts.fileops.events import FileOperationsEvent
from mote.contracts.ports.event_journal import UncommittedFact
from mote.runtime.session.events import (
    FILE_EDIT_PLAN_STORED,
    FILE_HISTORY_IMPORTED,
    FILE_TRANSACTION_ABORTED,
    FILE_TRANSACTION_COMMITTED,
    FILE_TRANSACTION_IN_DOUBT,
    FILE_TRANSACTION_PREPARED,
    HUNK_DETECTED,
    HUNK_REVIEW_TRANSITIONED,
    REWIND_ABORTED,
    REWIND_COMMITTED,
    REWIND_IN_DOUBT,
    REWIND_PREPARED,
    SESSION_EVENT_CLASSES,
    SessionEvent,
)

SESSION_FACT_SCHEMA_VERSION = 1
SESSION_STREAM_PREFIX = "session/"

_FILEOPS_EVENT_TYPES = frozenset(
    {
        FILE_EDIT_PLAN_STORED,
        FILE_HISTORY_IMPORTED,
        FILE_TRANSACTION_PREPARED,
        FILE_TRANSACTION_COMMITTED,
        FILE_TRANSACTION_ABORTED,
        FILE_TRANSACTION_IN_DOUBT,
        HUNK_DETECTED,
        HUNK_REVIEW_TRANSITIONED,
        REWIND_PREPARED,
        REWIND_COMMITTED,
        REWIND_ABORTED,
        REWIND_IN_DOUBT,
    }
)
_SAFE_LEGACY_TYPE = re.compile(r"^[a-z][a-z0-9_]*$")


class UnsupportedSessionFactVersion(ValueError):
    """A known session fact has no upcaster to the current payload model."""


def session_stream_id(session_id: str) -> str:
    if type(session_id) is not str or not session_id:
        raise ValueError("session_id must be a non-empty string")
    return f"{SESSION_STREAM_PREFIX}{session_id}"


def stable_event_type(legacy_type: str) -> EventType:
    if legacy_type not in SESSION_EVENT_CLASSES:
        raise KeyError(f"unknown session event discriminator: {legacy_type!r}")
    domain = "fileops" if legacy_type in _FILEOPS_EVENT_TYPES else "session"
    return EventType(f"mote.{domain}.{legacy_type}")


STABLE_SESSION_EVENT_CLASSES = MappingProxyType(
    {stable_event_type(legacy_type): event_class for legacy_type, event_class in SESSION_EVENT_CLASSES.items()}
)
_STABLE_FILEOPS_EVENT_TYPES = frozenset(stable_event_type(legacy_type) for legacy_type in _FILEOPS_EVENT_TYPES)


def encode_session_event(
    event: SessionEvent,
    *,
    session_id: str,
    occurred_at: Optional[datetime] = None,
    event_id: Optional[EventId] = None,
) -> UncommittedFact:
    """Encode one current typed session event as producer-owned fact data."""

    legacy_type = cast(str, event.type)
    if legacy_type not in SESSION_EVENT_CLASSES:
        raise TypeError(f"unsupported session event: {type(event).__name__}")
    occurred = occurred_at or datetime.now(timezone.utc)
    return UncommittedFact(
        event_id=event_id or EventId(str(uuid4())),
        event_type=stable_event_type(legacy_type),
        schema_version=SESSION_FACT_SCHEMA_VERSION,
        occurred_at=occurred,
        payload=cast(Mapping[str, JsonValue], event.payload()),
        session_id=session_id,
        run_id=_optional_text(event, "run_id"),
        turn_id=_optional_text(event, "turn_id"),
    )


def decode_session_event(
    envelope: EventEnvelope[Mapping[str, JsonValue]],
) -> Optional[SessionEvent]:
    """Decode one known current envelope; return None for another domain fact."""

    event_class = STABLE_SESSION_EVENT_CLASSES.get(envelope.event_type)
    if event_class is None:
        return None
    if envelope.schema_version != SESSION_FACT_SCHEMA_VERSION:
        raise UnsupportedSessionFactVersion(f"{envelope.event_type} schema {envelope.schema_version} has no upcaster")
    payload = thaw_json(cast(JsonValue, envelope.payload))
    if type(payload) is not dict:
        raise ValueError("session fact payload must decode to a JSON object")
    return cast(SessionEvent, event_class.from_payload(payload))


def iter_file_operations_events(
    envelopes: Iterable[EventEnvelope[Mapping[str, JsonValue]]],
) -> Iterator[FileOperationsEvent]:
    """Project known File Operations facts from a verified session stream."""

    for envelope in envelopes:
        if envelope.event_type not in _STABLE_FILEOPS_EVENT_TYPES:
            continue
        event = decode_session_event(envelope)
        if event is not None:
            yield cast(FileOperationsEvent, event)


def migrated_event_id(
    *,
    session_id: str,
    ordinal: int,
    legacy_type: str,
    timestamp: str,
    payload: Mapping[str, object],
) -> EventId:
    """Derive a repeatable identity for one legacy rollout record."""

    identity = json.dumps(
        {
            "legacy_type": legacy_type,
            "ordinal": ordinal,
            "payload": payload,
            "session_id": session_id,
            "timestamp": timestamp,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return EventId(hashlib.sha256(b"mote-session-v3\0" + identity).hexdigest())


def legacy_occurred_at(timestamp: str) -> datetime:
    """Interpret historic naive timestamps deterministically as UTC."""

    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError("legacy session timestamp is invalid") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def unknown_legacy_event_type(legacy_type: str) -> EventType:
    """Give an unknown historic discriminator a valid stable identity."""

    if _SAFE_LEGACY_TYPE.fullmatch(legacy_type) is not None:
        return EventType(f"mote.legacy.{legacy_type}")
    digest = hashlib.sha256(legacy_type.encode("utf-8")).hexdigest()
    return EventType(f"mote.legacy.unknown_{digest}")


def _optional_text(event: SessionEvent, name: str) -> Optional[str]:
    value: Any = getattr(event, name, None)
    return value if type(value) is str and value else None


__all__ = [
    "SESSION_FACT_SCHEMA_VERSION",
    "SESSION_STREAM_PREFIX",
    "STABLE_SESSION_EVENT_CLASSES",
    "UnsupportedSessionFactVersion",
    "decode_session_event",
    "encode_session_event",
    "legacy_occurred_at",
    "iter_file_operations_events",
    "migrated_event_id",
    "session_stream_id",
    "stable_event_type",
    "unknown_legacy_event_type",
]
