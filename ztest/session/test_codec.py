from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mote.contracts.events import EventEnvelope, EventId, EventType, StreamId
from mote.contracts.fileops.events import FileEditPlanStoredEvent
from mote.contracts.fileops.models import BlobRef
from mote.contracts.schema import UserMessage
from mote.runtime.session.codec import (
    SESSION_FACT_SCHEMA_VERSION,
    STABLE_SESSION_EVENT_CLASSES,
    UnsupportedSessionFactVersion,
    decode_session_event,
    encode_session_event,
    iter_file_operations_events,
    migrated_event_id,
    session_stream_id,
    stable_event_type,
    unknown_legacy_event_type,
)
from mote.runtime.session.events import MESSAGE, MessageEvent


def test_current_session_event_roundtrips_through_stable_fact_codec() -> None:
    event = MessageEvent(UserMessage(content="hello"))
    occurred_at = datetime(2026, 1, 2, tzinfo=timezone.utc)

    fact = encode_session_event(
        event,
        session_id="session-1",
        occurred_at=occurred_at,
        event_id=EventId("event-1"),
    )
    envelope = EventEnvelope(
        event_id=fact.event_id,
        event_type=fact.event_type,
        schema_version=fact.schema_version,
        stream_id=StreamId(session_stream_id("session-1")),
        sequence=1,
        occurred_at=fact.occurred_at,
        recorded_at=occurred_at,
        payload=fact.payload,
        session_id=fact.session_id,
    )

    restored = decode_session_event(envelope)

    assert fact.event_type == "mote.session.message"
    assert fact.schema_version == SESSION_FACT_SCHEMA_VERSION
    assert isinstance(restored, MessageEvent)
    assert restored.message.content == "hello"


def test_registry_has_one_stable_name_for_every_session_payload_class() -> None:
    assert len(STABLE_SESSION_EVENT_CLASSES) == len(set(STABLE_SESSION_EVENT_CLASSES.values()))
    assert stable_event_type(MESSAGE) in STABLE_SESSION_EVENT_CLASSES
    assert all(str(name).startswith("mote.") for name in STABLE_SESSION_EVENT_CLASSES)


def test_edit_plan_fact_is_projected_through_the_file_operations_port() -> None:
    event = FileEditPlanStoredEvent("plan-1", BlobRef("a" * 64, 12))
    timestamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    fact = encode_session_event(
        event,
        session_id="session-1",
        occurred_at=timestamp,
        event_id=EventId("edit-plan-1"),
    )
    envelope = EventEnvelope(
        event_id=fact.event_id,
        event_type=fact.event_type,
        schema_version=fact.schema_version,
        stream_id=StreamId(session_stream_id("session-1")),
        sequence=1,
        occurred_at=fact.occurred_at,
        recorded_at=timestamp,
        payload=fact.payload,
        session_id=fact.session_id,
    )

    assert fact.event_type == "mote.fileops.file_edit_plan_stored"
    assert tuple(iter_file_operations_events((envelope,))) == (event,)


def test_unknown_fact_is_not_misreported_as_a_known_session_event() -> None:
    timestamp = datetime.now(timezone.utc)
    envelope = EventEnvelope(
        event_id=EventId("unknown-1"),
        event_type=EventType("mote.extension.future"),
        schema_version=1,
        stream_id=StreamId("session/session-1"),
        sequence=1,
        occurred_at=timestamp,
        recorded_at=timestamp,
        payload={"kept": True},
    )

    assert decode_session_event(envelope) is None


def test_known_fact_with_future_schema_requires_an_explicit_upcaster() -> None:
    timestamp = datetime.now(timezone.utc)
    envelope = EventEnvelope(
        event_id=EventId("future-1"),
        event_type=stable_event_type(MESSAGE),
        schema_version=2,
        stream_id=StreamId("session/session-1"),
        sequence=1,
        occurred_at=timestamp,
        recorded_at=timestamp,
        payload={"content": "future"},
    )

    with pytest.raises(UnsupportedSessionFactVersion):
        decode_session_event(envelope)


def test_legacy_identity_is_deterministic_and_unknown_type_is_preserved() -> None:
    inputs = {
        "session_id": "legacy",
        "ordinal": 3,
        "legacy_type": "future_event",
        "timestamp": "2026-01-01T00:00:00",
        "payload": {"kept": True},
    }

    assert migrated_event_id(**inputs) == migrated_event_id(**inputs)
    assert unknown_legacy_event_type("future_event") == "mote.legacy.future_event"
    assert str(unknown_legacy_event_type("Future Event")).startswith("mote.legacy.unknown_")
