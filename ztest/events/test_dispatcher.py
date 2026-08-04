from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from mote.contracts.events.envelope import EventId, EventType, StreamId
from mote.contracts.ports.events.journal import AppendResult, UncommittedFact
from mote.contracts.ports.events.subscription import (
    CheckpointPolicy,
    DeadLetterEntry,
    EventFilter,
    Ordering,
    OverflowPolicy,
    Reliability,
    RetryPolicy,
    SubscriptionCheckpoint,
    SubscriptionIdentity,
    SubscriptionOwnerLease,
    SubscriptionSpec,
)
from mote.runtime.events.dispatcher import (
    CommittedEventDispatcher,
    DispatcherIntegrityError,
    DispatcherState,
    SubscriptionBinding,
    SubscriptionManifest,
)
from mote.runtime.events.journal import LocalEventJournal

_STREAM = StreamId("session/test")
_DURABLE = SubscriptionIdentity("mote.test.durable")
_LIVE = SubscriptionIdentity("mote.test.live")


def _fact(sequence: int, *, selected: bool = True) -> UncommittedFact:
    return UncommittedFact(
        event_id=EventId(f"event-{sequence}"),
        event_type=EventType("mote.test.selected" if selected else "mote.test.ignored"),
        schema_version=1,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        payload={"sequence": sequence},
    )


def _spec(identity: SubscriptionIdentity, reliability: Reliability) -> SubscriptionSpec:
    return SubscriptionSpec(
        identity=identity,
        event_filter=EventFilter(
            event_types=frozenset({EventType("mote.test.selected")}),
            stream_prefixes=("session/",),
        ),
        reliability=reliability,
        ordering=Ordering.PER_STREAM,
        capacity=4,
        overflow=(
            OverflowPolicy.BACKPRESSURE
            if reliability in {Reliability.DURABLE, Reliability.RELIABLE}
            else OverflowPolicy.DROP_NEWEST
        ),
        retry=RetryPolicy(
            max_attempts=1,
            initial_delay_seconds=0,
            maximum_delay_seconds=0,
            jitter_ratio=0,
        ),
        checkpoint=CheckpointPolicy(),
    )


class _StateStore:
    def __init__(self) -> None:
        self.values: dict[tuple[SubscriptionIdentity, StreamId], int] = {}

    async def load(self, identity, stream_id) -> int:
        return self.values.get((identity, stream_id), 0)

    async def claim_owner(self, identity, owner_id):
        return SubscriptionOwnerLease(identity, owner_id, 1, 1)

    async def save(self, checkpoint: SubscriptionCheckpoint) -> None:
        self.values[(checkpoint.identity, checkpoint.stream_id)] = checkpoint.sequence

    async def quarantine(
        self,
        entry: DeadLetterEntry,
        checkpoint: SubscriptionCheckpoint,
    ) -> None:
        await self.save(checkpoint)


class _Handler:
    def __init__(self) -> None:
        self.sequences: list[int] = []

    async def handle(self, envelope) -> None:
        self.sequences.append(envelope.sequence)


def _manifest(
    durable: _Handler,
    live: _Handler | None = None,
) -> SubscriptionManifest:
    bindings = [SubscriptionBinding(_spec(_DURABLE, Reliability.DURABLE), durable)]
    if live is not None:
        bindings.append(SubscriptionBinding(_spec(_LIVE, Reliability.LIVE), live))
    return SubscriptionManifest(tuple(bindings))


@pytest.mark.asyncio
async def test_start_replays_recoverable_subscription_and_advances_over_filter_gaps(
    tmp_path,
) -> None:
    journal = LocalEventJournal(tmp_path / "events.jsonl", _STREAM)
    await journal.append(
        _STREAM,
        (_fact(1), _fact(2, selected=False), _fact(3)),
        expected_version=0,
    )
    durable = _Handler()
    live = _Handler()
    store = _StateStore()
    dispatcher = CommittedEventDispatcher(
        journal,
        _manifest(durable, live),
        state_store=store,
    )

    await dispatcher.start((_STREAM,))

    assert dispatcher.state is DispatcherState.RUNNING
    assert dispatcher.cursor(_STREAM) == 3
    assert durable.sequences == [1, 3]
    assert live.sequences == []
    assert store.values[(_DURABLE, _STREAM)] == 3
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_dispatch_and_explicit_barrier_publish_only_committed_envelopes(
    tmp_path,
) -> None:
    journal = LocalEventJournal(tmp_path / "events.jsonl", _STREAM)
    handler = _Handler()
    dispatcher = CommittedEventDispatcher(
        journal,
        _manifest(handler),
        state_store=_StateStore(),
    )
    await dispatcher.start((_STREAM,))
    result = await journal.append(_STREAM, (_fact(1),), expected_version=0)

    await dispatcher.dispatch(result)
    await dispatcher.wait_until(_DURABLE, _STREAM, 1)

    assert handler.sequences == [1]
    assert dispatcher.cursor(_STREAM) == 1
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_reconcile_closes_commit_before_dispatch_gap(tmp_path) -> None:
    journal = LocalEventJournal(tmp_path / "events.jsonl", _STREAM)
    handler = _Handler()
    dispatcher = CommittedEventDispatcher(
        journal,
        _manifest(handler),
        state_store=_StateStore(),
    )
    await dispatcher.start((_STREAM,))
    await journal.append(_STREAM, (_fact(1),), expected_version=0)

    assert await dispatcher.reconcile(_STREAM) == 1
    await dispatcher.wait_until(_DURABLE, _STREAM, 1)

    assert handler.sequences == [1]
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_dispatch_rejects_noncontiguous_append_result(tmp_path) -> None:
    journal = LocalEventJournal(tmp_path / "events.jsonl", _STREAM)
    dispatcher = CommittedEventDispatcher(
        journal,
        _manifest(_Handler()),
        state_store=_StateStore(),
    )
    await dispatcher.start((_STREAM,))
    committed = await journal.append(_STREAM, (_fact(1),), expected_version=0)
    invalid = AppendResult(
        stream_id=_STREAM,
        previous_version=1,
        current_version=2,
        envelopes=committed.envelopes,
    )

    with pytest.raises(DispatcherIntegrityError):
        await dispatcher.dispatch(invalid)
    await dispatcher.aclose()


def test_manifest_rejects_duplicate_subscription_identity() -> None:
    spec = _spec(_DURABLE, Reliability.DURABLE)
    handler = _Handler()

    with pytest.raises(ValueError, match="duplicate identities"):
        SubscriptionManifest(
            (
                SubscriptionBinding(spec, handler),
                SubscriptionBinding(spec, handler),
            )
        )


@pytest.mark.asyncio
async def test_close_leaves_no_subscription_owner_tasks(tmp_path) -> None:
    journal = LocalEventJournal(tmp_path / "events.jsonl", _STREAM)
    dispatcher = CommittedEventDispatcher(
        journal,
        _manifest(_Handler(), _Handler()),
        state_store=_StateStore(),
    )
    await dispatcher.start((_STREAM,))

    await dispatcher.aclose()
    await asyncio.sleep(0)

    assert dispatcher.state is DispatcherState.CLOSED
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and task.get_name().startswith("event-subscription:") and not task.done()
    ]
