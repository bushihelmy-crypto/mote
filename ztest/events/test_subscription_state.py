from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mote.contracts.events.envelope import EventEnvelope, EventId, EventType, StreamId
from mote.contracts.ports.events.subscription import DeadLetterEntry, SubscriptionCheckpoint, SubscriptionIdentity
from mote.runtime.events.backends.subscription_state import (
    CheckpointRegressionError,
    SQLiteSubscriptionStateStore,
    SubscriptionStateStoreClosed,
)


def _event(sequence: int = 1) -> EventEnvelope:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return EventEnvelope(
        event_id=EventId(f"event-{sequence}"),
        event_type=EventType("mote.test.fact"),
        schema_version=1,
        stream_id=StreamId("session/test"),
        sequence=sequence,
        occurred_at=timestamp,
        recorded_at=timestamp,
        payload={"sequence": sequence},
    )


def _checkpoint(sequence: int) -> SubscriptionCheckpoint:
    return SubscriptionCheckpoint(
        identity=SubscriptionIdentity("mote.test.subscription"),
        stream_id=StreamId("session/test"),
        sequence=sequence,
    )


def _dead_letter(sequence: int = 1) -> DeadLetterEntry:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return DeadLetterEntry(
        subscription=SubscriptionIdentity("mote.test.subscription"),
        envelope=_event(sequence),
        attempts=3,
        error="RuntimeError: poison",
        first_failed_at=timestamp,
        last_failed_at=timestamp,
    )


@pytest.mark.asyncio
async def test_constructor_is_pure_and_closed_store_rejects_operations(
    tmp_path,
) -> None:
    path = tmp_path / "state" / "subscriptions.sqlite3"
    store = SQLiteSubscriptionStateStore(path)

    assert not path.exists()
    with pytest.raises(SubscriptionStateStoreClosed):
        await store.load(
            SubscriptionIdentity("mote.test.subscription"),
            StreamId("session/test"),
        )


@pytest.mark.asyncio
async def test_checkpoint_survives_reopen_and_cannot_regress(tmp_path) -> None:
    path = tmp_path / "subscriptions.sqlite3"
    store = SQLiteSubscriptionStateStore(path)
    await store.aopen()
    await store.save(_checkpoint(2))

    assert (
        await store.load(
            SubscriptionIdentity("mote.test.subscription"),
            StreamId("session/test"),
        )
        == 2
    )
    with pytest.raises(CheckpointRegressionError):
        await store.save(_checkpoint(1))
    await store.aclose()

    reopened = SQLiteSubscriptionStateStore(path)
    await reopened.aopen()
    assert (
        await reopened.load(
            SubscriptionIdentity("mote.test.subscription"),
            StreamId("session/test"),
        )
        == 2
    )
    await reopened.aclose()


@pytest.mark.asyncio
async def test_quarantine_atomically_persists_replayable_event_and_checkpoint(
    tmp_path,
) -> None:
    store = SQLiteSubscriptionStateStore(tmp_path / "subscriptions.sqlite3")
    await store.aopen()
    entry = _dead_letter()

    await store.quarantine(entry, _checkpoint(1))
    await store.quarantine(entry, _checkpoint(1))

    assert (
        await store.load(
            SubscriptionIdentity("mote.test.subscription"),
            StreamId("session/test"),
        )
        == 1
    )
    assert await store.list_dead_letters() == (entry,)
    await store.aclose()


@pytest.mark.asyncio
async def test_failed_checkpoint_advance_rolls_back_dead_letter_insert(
    tmp_path,
) -> None:
    store = SQLiteSubscriptionStateStore(tmp_path / "subscriptions.sqlite3")
    await store.aopen()
    await store.save(_checkpoint(2))

    with pytest.raises(CheckpointRegressionError):
        await store.quarantine(_dead_letter(1), _checkpoint(1))

    assert await store.list_dead_letters() == ()
    assert (
        await store.load(
            SubscriptionIdentity("mote.test.subscription"),
            StreamId("session/test"),
        )
        == 2
    )
    await store.aclose()
