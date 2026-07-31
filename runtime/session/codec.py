"""Stable session fact names and typed payload encoding for journal envelopes."""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Optional, cast
from uuid import uuid4

from mote.contracts.events.envelope import EventEnvelope, EventId, EventType, JsonValue, thaw_json
from mote.contracts.events.file.facts import FileOperationsEvent
from mote.contracts.events.governance import (
    ArtifactPolicy,
    CodecState,
    CompactionDisposition,
    EventCodecEntry,
    Sensitivity,
    StoragePolicy,
)
from mote.contracts.ports.events.journal import UncommittedFact
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


class UnsupportedSessionFactVersion(ValueError):
    """A session fact does not use the one current envelope version."""


class UnsupportedSessionEventError(ValueError):
    """A session stream contains an event outside the current tagged union."""


def session_stream_id(session_id: str) -> str:
    if type(session_id) is not str or not session_id:
        raise ValueError("session_id must be a non-empty string")
    return f"{SESSION_STREAM_PREFIX}{session_id}"


def stable_event_type(event_type: str) -> EventType:
    if event_type not in SESSION_EVENT_CLASSES:
        raise KeyError(f"unknown session event discriminator: {event_type!r}")
    domain = "fileops" if event_type in _FILEOPS_EVENT_TYPES else "session"
    return EventType(f"mote.{domain}.{event_type}")


STABLE_SESSION_EVENT_CLASSES = MappingProxyType(
    {stable_event_type(event_type): event_class for event_type, event_class in SESSION_EVENT_CLASSES.items()}
)
_STABLE_FILEOPS_EVENT_TYPES = frozenset(stable_event_type(event_type) for event_type in _FILEOPS_EVENT_TYPES)

SESSION_STORAGE_POLICY = StoragePolicy(
    sensitivity=Sensitivity.RESTRICTED,
    semantic_inline_size_limit=1024 * 1024,
    retention_requirement="session-scoped; stream deletion releases artifact ownership first",
    redaction_at_source=True,
    compaction_disposition=CompactionDisposition.STREAM_DELETE,
    legal_hold_behavior="retain the complete session stream while the hold is active",
    artifact_policy=ArtifactPolicy.REFERENCES_ONLY,
    secondary_copy_policy="diagnostics and quarantine store identifiers and redacted excerpts only",
)


def _validate_catalog_event(event: SessionEvent) -> None:
    if event.type not in SESSION_EVENT_CLASSES:
        raise TypeError(f"unsupported session event: {type(event).__name__}")


def encode_session_event(
    event: SessionEvent,
    *,
    session_id: str,
    occurred_at: Optional[datetime] = None,
    event_id: Optional[EventId] = None,
) -> UncommittedFact:
    """Encode one current typed session event as producer-owned fact data."""

    event_type = cast(str, event.type)
    if event_type not in SESSION_EVENT_CLASSES:
        raise TypeError(f"unsupported session event: {type(event).__name__}")
    occurred = occurred_at or datetime.now(timezone.utc)
    return UncommittedFact(
        event_id=event_id or EventId(str(uuid4())),
        event_type=stable_event_type(event_type),
        schema_version=SESSION_FACT_SCHEMA_VERSION,
        occurred_at=occurred,
        payload=cast(Mapping[str, JsonValue], event.payload()),
        session_id=session_id,
        run_id=_optional_text(event, "run_id"),
        turn_id=_optional_text(event, "turn_id"),
    )


def decode_session_event(
    envelope: EventEnvelope[Mapping[str, JsonValue]],
) -> SessionEvent:
    """Decode one current session envelope."""

    event_class = STABLE_SESSION_EVENT_CLASSES.get(envelope.event_type)
    if event_class is None:
        raise UnsupportedSessionEventError(f"[unsupported_session_event] event_type={envelope.event_type}")
    if envelope.schema_version != SESSION_FACT_SCHEMA_VERSION:
        raise UnsupportedSessionFactVersion(
            "[unsupported_session_fact_version] "
            f"expected={SESSION_FACT_SCHEMA_VERSION} actual={envelope.schema_version}"
        )
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
        yield cast(FileOperationsEvent, event)


def _optional_text(event: SessionEvent, name: str) -> Optional[str]:
    value: Any = getattr(event, name, None)
    return value if type(value) is str and value else None


SESSION_ACTIVE_CODECS = tuple(
    EventCodecEntry(
        logical_store="session-rollout",
        event_family=event_type,
        event_type=stable_event_type(event_type),
        event_schema_version=SESSION_FACT_SCHEMA_VERSION,
        store_generation=1,
        state=CodecState.ACTIVE,
        owner_id=("file-operations" if event_type in _FILEOPS_EVENT_TYPES else "session"),
        encoder=encode_session_event,
        decoder=decode_session_event,
        validator=_validate_catalog_event,
        policy=SESSION_STORAGE_POLICY,
    )
    for event_type in SESSION_EVENT_CLASSES
)


__all__ = [
    "SESSION_FACT_SCHEMA_VERSION",
    "SESSION_ACTIVE_CODECS",
    "SESSION_STORAGE_POLICY",
    "SESSION_STREAM_PREFIX",
    "STABLE_SESSION_EVENT_CLASSES",
    "UnsupportedSessionFactVersion",
    "UnsupportedSessionEventError",
    "decode_session_event",
    "encode_session_event",
    "iter_file_operations_events",
    "session_stream_id",
    "stable_event_type",
]
