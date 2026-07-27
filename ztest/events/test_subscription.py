from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mote.contracts.events import EventEnvelope, EventId, EventType, StreamId
from mote.contracts.ports.event_subscription import (
    CheckpointPolicy,
    DeadLetterEntry,
    EventFilter,
    Ordering,
    OverflowPolicy,
    Reliability,
    RetryPolicy,
    SubscriptionCheckpoint,
    SubscriptionIdentity,
    SubscriptionSpec,
)
from mote.runtime.events.subscription import SubscriptionFailed, SubscriptionState, SubscriptionWorker


def _event(sequence: int) -> EventEnvelope:
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


def _spec(
    reliability: Reliability,
    *,
    attempts: int = 3,
    persist_every: int = 1,
) -> SubscriptionSpec:
    return SubscriptionSpec(
        identity=SubscriptionIdentity("mote.test.subscription"),
        event_filter=EventFilter(),
        reliability=reliability,
        ordering=Ordering.PER_STREAM,
        capacity=4,
        overflow=(
            OverflowPolicy.BACKPRESSURE
            if reliability in {Reliability.DURABLE, Reliability.RELIABLE}
            else OverflowPolicy.DROP_NEWEST
        ),
        retry=RetryPolicy(
            max_attempts=attempts,
            initial_delay_seconds=0,
            maximum_delay_seconds=0,
            jitter_ratio=0,
        ),
        checkpoint=CheckpointPolicy(persist_every=persist_every),
    )


class _CheckpointStore:
    def __init__(self) -> None:
        self.values = {}
        self.saves: list[SubscriptionCheckpoint] = []
        self.entries: list[DeadLetterEntry] = []

    async def load(self, identity, stream_id) -> int:
        return self.values.get((identity, stream_id), 0)

    async def save(self, checkpoint: SubscriptionCheckpoint) -> None:
        self.values[(checkpoint.identity, checkpoint.stream_id)] = checkpoint.sequence
        self.saves.append(checkpoint)

    async def quarantine(
        self,
        entry: DeadLetterEntry,
        checkpoint: SubscriptionCheckpoint,
    ) -> None:
        self.entries.append(entry)
        await self.save(checkpoint)


class _Handler:
    def __init__(self, failures: dict[int, int] | None = None) -> None:
        self.failures = dict(failures or {})
        self.attempts: list[int] = []
        self.handled: list[int] = []

    async def handle(self, envelope) -> None:
        sequence = envelope.sequence
        self.attempts.append(sequence)
        remaining = self.failures.get(sequence, 0)
        if remaining:
            self.failures[sequence] = remaining - 1
            raise RuntimeError(f"failure-{sequence}")
        self.handled.append(sequence)


@pytest.mark.asyncio
async def test_durable_worker_checkpoints_then_crosses_barrier() -> None:
    checkpoints = _CheckpointStore()
    handler = _Handler()
    worker = SubscriptionWorker(
        _spec(Reliability.DURABLE),
        handler,
        state_store=checkpoints,
    )
    await worker.start()

    await worker.publish(_event(1))
    await worker.wait_until(StreamId("session/test"), 1)

    assert handler.handled == [1]
    assert checkpoints.saves[-1].sequence == 1
    await worker.aclose()
    assert worker.state is SubscriptionState.CLOSED


@pytest.mark.asyncio
async def test_retry_succeeds_without_duplicate_checkpoint() -> None:
    checkpoints = _CheckpointStore()
    handler = _Handler({1: 1})
    worker = SubscriptionWorker(
        _spec(Reliability.DURABLE, attempts=2),
        handler,
        state_store=checkpoints,
    )
    await worker.start()

    await worker.publish(_event(1))
    await worker.wait_until(StreamId("session/test"), 1)

    assert handler.attempts == [1, 1]
    assert [item.sequence for item in checkpoints.saves] == [1]
    await worker.aclose()


@pytest.mark.asyncio
async def test_durable_poison_event_fails_without_advancing() -> None:
    checkpoints = _CheckpointStore()
    handler = _Handler({1: 2})
    worker = SubscriptionWorker(
        _spec(Reliability.DURABLE, attempts=2),
        handler,
        state_store=checkpoints,
    )
    await worker.start()
    await worker.publish(_event(1))

    with pytest.raises(SubscriptionFailed):
        await worker.wait_until(StreamId("session/test"), 1)

    assert worker.state is SubscriptionState.FAILED
    assert checkpoints.saves == []
    await worker.aclose(drain=False)


@pytest.mark.asyncio
async def test_reliable_poison_event_enters_dlq_and_does_not_block_tail() -> None:
    checkpoints = _CheckpointStore()
    handler = _Handler({1: 2})
    worker = SubscriptionWorker(
        _spec(Reliability.RELIABLE, attempts=2),
        handler,
        state_store=checkpoints,
    )
    await worker.start()
    await worker.publish(_event(1))
    await worker.publish(_event(2))

    await worker.wait_until(StreamId("session/test"), 2)

    assert worker.state is SubscriptionState.DEGRADED
    assert handler.handled == [2]
    assert len(checkpoints.entries) == 1
    assert checkpoints.entries[0].sequence == 1
    assert checkpoints.entries[0].envelope == _event(1)
    assert checkpoints.saves[-1].sequence == 2
    await worker.aclose()


@pytest.mark.asyncio
async def test_drain_flushes_batched_checkpoint() -> None:
    checkpoints = _CheckpointStore()
    worker = SubscriptionWorker(
        _spec(Reliability.DURABLE, persist_every=10),
        _Handler(),
        state_store=checkpoints,
    )
    await worker.start()
    await worker.publish(_event(1))
    await worker.wait_until(StreamId("session/test"), 1)
    assert checkpoints.saves == []

    await worker.drain()

    assert checkpoints.saves[-1].sequence == 1
    await worker.aclose()


@pytest.mark.asyncio
async def test_persisted_checkpoint_skips_duplicate_delivery() -> None:
    checkpoints = _CheckpointStore()
    identity = SubscriptionIdentity("mote.test.subscription")
    stream_id = StreamId("session/test")
    checkpoints.values[(identity, stream_id)] = 1
    handler = _Handler()
    worker = SubscriptionWorker(
        _spec(Reliability.DURABLE),
        handler,
        state_store=checkpoints,
    )
    await worker.start()

    await worker.publish(_event(1))
    await worker.wait_until(stream_id, 1)

    assert handler.attempts == []
    await worker.aclose()
