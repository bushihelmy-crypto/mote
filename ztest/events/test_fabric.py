from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from mote.contracts.events import EventId, EventType, StreamId
from mote.contracts.ports.event_journal import UncommittedFact
from mote.contracts.ports.event_subscription import (
    EventFilter,
    Ordering,
    OverflowPolicy,
    Reliability,
    RetryPolicy,
    SubscriptionIdentity,
    SubscriptionSpec,
)
from mote.contracts.ports.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.runtime.events.backends.subscription_state import SQLiteSubscriptionStateStore
from mote.runtime.events.dispatcher import SubscriptionBinding, SubscriptionManifest
from mote.runtime.events.fabric import EventFabric, EventFabricReadOnly, EventFabricUnavailable
from mote.runtime.events.health import FabricHealth, FabricHealthState, FabricState
from mote.runtime.events.journal import LocalEventJournal
from mote.runtime.events.telemetry import TelemetryBinding, TelemetryManifest, TelemetryRuntime

_STREAM = StreamId("session/test")
_SUBSCRIPTION = SubscriptionIdentity("mote.test.projection")


def _fact(sequence: int) -> UncommittedFact:
    return UncommittedFact(
        event_id=EventId(f"event-{sequence}"),
        event_type=EventType("mote.test.fact"),
        schema_version=1,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        payload={"sequence": sequence},
    )


def _spec(reliability: Reliability) -> SubscriptionSpec:
    return SubscriptionSpec(
        identity=_SUBSCRIPTION,
        event_filter=EventFilter(),
        reliability=reliability,
        ordering=Ordering.PER_STREAM,
        capacity=4,
        overflow=OverflowPolicy.BACKPRESSURE,
        retry=RetryPolicy(
            max_attempts=1,
            attempt_timeout_seconds=1,
            initial_delay_seconds=0,
            maximum_delay_seconds=0,
            jitter_ratio=0,
        ),
    )


class _Handler:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sequences: list[int] = []

    async def handle(self, envelope) -> None:
        if self.fail:
            raise RuntimeError("projection failed")
        self.sequences.append(envelope.sequence)


def _fabric(
    tmp_path,
    handler: _Handler,
    *,
    reliability: Reliability = Reliability.DURABLE,
    health: FabricHealth | None = None,
    telemetry: TelemetryRuntime | None = None,
) -> tuple[EventFabric, LocalEventJournal, SQLiteSubscriptionStateStore]:
    journal = LocalEventJournal(tmp_path / "events.jsonl", _STREAM)
    state_store = SQLiteSubscriptionStateStore(tmp_path / "subscriptions.sqlite3")
    fabric = EventFabric(
        journal=journal,
        streams=(_STREAM,),
        subscriptions=SubscriptionManifest((SubscriptionBinding(_spec(reliability), handler),)),
        state_store=state_store,
        health=health,
        telemetry=telemetry,
    )
    return fabric, journal, state_store


@pytest.mark.asyncio
async def test_fabric_owns_explicit_start_append_barrier_and_close(tmp_path) -> None:
    handler = _Handler()
    fabric, _, state_store = _fabric(tmp_path, handler)

    assert fabric.state is FabricState.NEW
    assert not state_store.path.exists()
    await fabric.start()
    result = await fabric.append(_STREAM, (_fact(1),))
    await fabric.wait_until(_SUBSCRIPTION, _STREAM, result.last_sequence)

    snapshot = fabric.health_snapshot()
    assert snapshot.state is FabricHealthState.HEALTHY
    assert snapshot.ready
    assert snapshot.writable
    assert snapshot.subscriptions[0].streams[0].delivery_lag == 0
    assert snapshot.subscriptions[0].streams[0].durable_lag == 0
    assert handler.sequences == [1]

    await fabric.aclose()
    assert fabric.state is FabricState.CLOSED
    with pytest.raises(EventFabricUnavailable):
        await fabric.append(_STREAM, (_fact(2),))


@pytest.mark.asyncio
async def test_concurrent_producers_commit_and_dispatch_in_stream_order(
    tmp_path,
) -> None:
    handler = _Handler()
    fabric, _, _ = _fabric(tmp_path, handler)
    await fabric.start()

    first = asyncio.create_task(fabric.append(_STREAM, (_fact(1),)))
    await asyncio.sleep(0)
    second = asyncio.create_task(fabric.append(_STREAM, (_fact(2),)))
    results = await asyncio.gather(first, second)
    await fabric.wait_until(_SUBSCRIPTION, _STREAM, 2)

    assert [result.current_version for result in results] == [1, 2]
    assert handler.sequences == [1, 2]
    await fabric.aclose()


@pytest.mark.asyncio
async def test_worker_thread_append_uses_owner_loop_and_supports_barrier(
    tmp_path,
) -> None:
    handler = _Handler()
    fabric, _, _ = _fabric(tmp_path, handler)
    await fabric.start()

    result = await asyncio.to_thread(
        fabric.append_from_thread,
        _STREAM,
        (_fact(1),),
    )
    await fabric.wait_until(_SUBSCRIPTION, _STREAM, result.last_sequence)

    assert result.current_version == 1
    assert handler.sequences == [1]
    await fabric.aclose()


@pytest.mark.asyncio
async def test_thread_append_rejects_owner_loop_to_prevent_deadlock(tmp_path) -> None:
    fabric, _, _ = _fabric(tmp_path, _Handler())
    await fabric.start()

    with pytest.raises(RuntimeError, match="non-owner thread"):
        fabric.append_from_thread(_STREAM, (_fact(1),))

    await fabric.aclose()


@pytest.mark.asyncio
async def test_thread_append_rejects_closed_fabric(tmp_path) -> None:
    fabric, _, _ = _fabric(tmp_path, _Handler())
    await fabric.start()
    await fabric.aclose()

    with pytest.raises(EventFabricUnavailable):
        await asyncio.to_thread(
            fabric.append_from_thread,
            _STREAM,
            (_fact(1),),
        )


@pytest.mark.asyncio
async def test_durable_projection_failure_makes_fabric_unavailable_before_next_commit(
    tmp_path,
) -> None:
    fabric, journal, _ = _fabric(tmp_path, _Handler(fail=True))
    await fabric.start()
    await fabric.append(_STREAM, (_fact(1),))

    with pytest.raises(Exception):
        await fabric.wait_until(_SUBSCRIPTION, _STREAM, 1)

    snapshot = fabric.health_snapshot()
    assert snapshot.state is FabricHealthState.UNAVAILABLE
    assert not snapshot.ready
    assert not snapshot.writable
    with pytest.raises(EventFabricUnavailable):
        await fabric.append(_STREAM, (_fact(2),))
    assert (await journal.verify(_STREAM)).current_version == 1
    await fabric.aclose()


@pytest.mark.asyncio
async def test_reliable_poison_event_degrades_but_remains_writable(tmp_path) -> None:
    fabric, _, state_store = _fabric(
        tmp_path,
        _Handler(fail=True),
        reliability=Reliability.RELIABLE,
    )
    await fabric.start()
    await fabric.append(_STREAM, (_fact(1),))
    await fabric.wait_until(_SUBSCRIPTION, _STREAM, 1)

    snapshot = fabric.health_snapshot()
    assert snapshot.state is FabricHealthState.DEGRADED
    assert snapshot.ready
    assert snapshot.writable
    assert (await state_store.list_dead_letters())[0].sequence == 1
    await fabric.aclose()


@pytest.mark.asyncio
async def test_manual_read_only_health_blocks_writes_without_blocking_reads(
    tmp_path,
) -> None:
    health = FabricHealth()
    fabric, journal, _ = _fabric(tmp_path, _Handler(), health=health)
    await fabric.start()
    health.mark_read_only("fabric.journal", "disk is not writable")

    snapshot = fabric.health_snapshot()
    assert snapshot.state is FabricHealthState.READ_ONLY
    assert snapshot.ready
    assert not snapshot.writable
    with pytest.raises(EventFabricReadOnly):
        await fabric.append(_STREAM, (_fact(1),))
    assert (await journal.verify(_STREAM)).current_version == 0
    await fabric.aclose()


@pytest.mark.asyncio
async def test_telemetry_failure_degrades_health_without_blocking_writes(
    tmp_path,
) -> None:
    telemetry = TelemetryRuntime(
        TelemetryManifest(
            (
                TelemetryBinding(
                    TelemetrySubscriptionSpec(
                        identity=TelemetryIdentity("mote.test.health_telemetry"),
                        capacity=4,
                        overflow=TelemetryOverflow.DROP_NEWEST,
                    ),
                    _Handler(fail=True),
                ),
            )
        )
    )
    telemetry.start()
    fabric, _, _ = _fabric(tmp_path, _Handler(), telemetry=telemetry)
    await fabric.start()
    await telemetry.emit(object())
    await telemetry.drain()

    snapshot = fabric.health_snapshot()
    assert snapshot.state is FabricHealthState.DEGRADED
    assert snapshot.ready
    assert snapshot.writable
    assert snapshot.telemetry[0].failures == 1
    await fabric.append(_STREAM, (_fact(1),))

    await fabric.aclose()
    await telemetry.aclose()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_leaves_no_fabric_tasks(tmp_path) -> None:
    fabric, _, _ = _fabric(tmp_path, _Handler())
    await fabric.start()

    await fabric.aclose()
    await fabric.aclose()
    await asyncio.sleep(0)

    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and (task.get_name().startswith("event-fabric-") or task.get_name().startswith("event-subscription:"))
        and not task.done()
    ]
