from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from mote.contracts.events import EventEnvelope, EventId, EventType, StreamId
from mote.contracts.ports.event_subscription import (
    EventFilter,
    Ordering,
    OverflowPolicy,
    Reliability,
    SubscriptionIdentity,
    SubscriptionSpec,
)
from mote.runtime.events.mailbox import MailboxClosed, MailboxPutResult, SubscriptionMailbox


def _event(
    sequence: int,
    *,
    event_type: str = "mote.test.fact",
    stream_id: str = "session/test",
) -> EventEnvelope:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return EventEnvelope(
        event_id=EventId(f"event-{sequence}-{event_type}"),
        event_type=EventType(event_type),
        schema_version=1,
        stream_id=StreamId(stream_id),
        sequence=sequence,
        occurred_at=timestamp,
        recorded_at=timestamp,
        payload={"sequence": sequence},
    )


def _spec(reliability: Reliability, overflow: OverflowPolicy) -> SubscriptionSpec:
    return SubscriptionSpec(
        identity=SubscriptionIdentity("mote.test.subscription"),
        event_filter=EventFilter(),
        reliability=reliability,
        ordering=Ordering.PER_STREAM,
        capacity=2,
        overflow=overflow,
    )


def test_subscription_reliability_constrains_overflow() -> None:
    _spec(Reliability.DURABLE, OverflowPolicy.BACKPRESSURE)
    _spec(Reliability.LIVE, OverflowPolicy.COALESCE)

    with pytest.raises(ValueError, match="must use backpressure"):
        _spec(Reliability.DURABLE, OverflowPolicy.DROP_OLDEST)
    with pytest.raises(ValueError, match="must not backpressure"):
        _spec(Reliability.LOSSY, OverflowPolicy.BACKPRESSURE)


@pytest.mark.asyncio
async def test_backpressure_waits_for_capacity() -> None:
    mailbox = SubscriptionMailbox(
        capacity=1,
        overflow=OverflowPolicy.BACKPRESSURE,
    )
    await mailbox.put(_event(1))
    blocked = asyncio.create_task(mailbox.put(_event(2)))
    await asyncio.sleep(0)
    assert not blocked.done()

    first = await mailbox.get()
    await mailbox.task_done()

    assert first.sequence == 1
    assert await blocked is MailboxPutResult.ENQUEUED
    second = await mailbox.get()
    await mailbox.task_done()
    await mailbox.join()
    assert second.sequence == 2


@pytest.mark.asyncio
async def test_drop_newest_preserves_queued_event() -> None:
    mailbox = SubscriptionMailbox(
        capacity=1,
        overflow=OverflowPolicy.DROP_NEWEST,
    )

    assert await mailbox.put(_event(1)) is MailboxPutResult.ENQUEUED
    assert await mailbox.put(_event(2)) is MailboxPutResult.DROPPED
    assert (await mailbox.get()).sequence == 1
    await mailbox.task_done()
    assert mailbox.snapshot().dropped == 1


@pytest.mark.asyncio
async def test_drop_oldest_releases_drain_accounting() -> None:
    mailbox = SubscriptionMailbox(
        capacity=1,
        overflow=OverflowPolicy.DROP_OLDEST,
    )
    await mailbox.put(_event(1))
    await mailbox.put(_event(2))

    assert (await mailbox.get()).sequence == 2
    await mailbox.task_done()
    await mailbox.join()
    assert mailbox.snapshot().dropped == 1


@pytest.mark.asyncio
async def test_coalesce_keeps_latest_event_per_stream_and_type() -> None:
    mailbox = SubscriptionMailbox(
        capacity=3,
        overflow=OverflowPolicy.COALESCE,
    )
    await mailbox.put(_event(1))

    result = await mailbox.put(_event(2))

    assert result is MailboxPutResult.COALESCED
    assert (await mailbox.get()).sequence == 2
    await mailbox.task_done()
    snapshot = mailbox.snapshot()
    assert snapshot.coalesced == 1
    assert snapshot.unfinished == 0


@pytest.mark.asyncio
async def test_closed_mailbox_drains_then_stops() -> None:
    mailbox = SubscriptionMailbox(
        capacity=1,
        overflow=OverflowPolicy.DROP_NEWEST,
    )
    await mailbox.put(_event(1))
    await mailbox.close()

    assert (await mailbox.get()).sequence == 1
    await mailbox.task_done()
    with pytest.raises(MailboxClosed):
        await mailbox.get()
    with pytest.raises(MailboxClosed):
        await mailbox.put(_event(2))
