from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TypeGuard

import pytest

from mote.contracts.ports.events.telemetry import (
    MAX_TELEMETRY_IDENTITY_BYTES,
    TelemetryIdentity,
    TelemetryOverflow,
    TelemetrySubscriptionSpec,
)
from mote.runtime.events.telemetry import (
    TelemetryBinding,
    TelemetryManifest,
    TelemetryRuntime,
    TelemetryState,
    TypedTelemetryBinding,
)


@dataclass(frozen=True)
class _Event:
    value: int
    name: str = "test.event"


def _binding(
    identity: str,
    handler,
    *,
    capacity: int = 4,
    overflow: TelemetryOverflow = TelemetryOverflow.DROP_NEWEST,
) -> TelemetryBinding:
    return TelemetryBinding(
        TelemetrySubscriptionSpec(
            identity=TelemetryIdentity(identity),
            capacity=capacity,
            overflow=overflow,
        ),
        handler,
    )


def _accepts_event(event: object) -> TypeGuard[_Event]:
    return isinstance(event, _Event)


class _Handler:
    def __init__(self) -> None:
        self.values: list[int] = []

    async def handle(self, event: object) -> None:
        assert isinstance(event, _Event)
        self.values.append(event.value)


class _BlockingHandler(_Handler):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(self, event: object) -> None:
        assert isinstance(event, _Event)
        self.values.append(event.value)
        if len(self.values) == 1:
            self.entered.set()
            await self.release.wait()


class _SyncHandler(_Handler):
    def handle_sync(self, event: object) -> None:
        assert isinstance(event, _Event)
        self.values.append(event.value)


class _FailingHandler(_Handler):
    async def handle(self, event: object) -> None:
        assert isinstance(event, _Event)
        if event.value == 1:
            raise RuntimeError("telemetry failure")
        self.values.append(event.value)


@pytest.mark.asyncio
async def test_emit_never_awaits_slow_subscriber_and_mailbox_is_bounded() -> None:
    slow = _BlockingHandler()
    fast = _Handler()
    runtime = TelemetryRuntime(
        TelemetryManifest(
            (
                _binding("mote.test.slow", slow, capacity=1),
                _binding("mote.test.fast", fast),
            )
        )
    )
    runtime.start()
    await runtime.emit(_Event(1))
    await slow.entered.wait()

    await runtime.emit(_Event(2))
    await runtime.emit(_Event(3))
    slow.release.set()
    await runtime.drain()

    snapshots = {item.identity: item for item in runtime.snapshots()}
    assert slow.values == [1, 2]
    assert fast.values == [1, 2, 3]
    assert snapshots[TelemetryIdentity("mote.test.slow")].mailbox.dropped == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_coalesce_keeps_latest_pending_observation() -> None:
    handler = _BlockingHandler()
    runtime = TelemetryRuntime(
        TelemetryManifest(
            (
                _binding(
                    "mote.test.coalesce",
                    handler,
                    capacity=1,
                    overflow=TelemetryOverflow.COALESCE,
                ),
            )
        )
    )
    runtime.start()
    await runtime.emit(_Event(1))
    await handler.entered.wait()
    await runtime.emit(_Event(2))
    await runtime.emit(_Event(3))
    handler.release.set()
    await runtime.drain()

    assert handler.values == [1, 3]
    assert runtime.snapshots()[0].mailbox.coalesced == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_sync_emit_is_enqueued_and_processed_by_owner_task() -> None:
    handler = _SyncHandler()
    typed = TypedTelemetryBinding(
        TelemetrySubscriptionSpec(
            TelemetryIdentity("mote.test.sync"),
            4,
            TelemetryOverflow.DROP_NEWEST,
        ),
        _accepts_event,
        handler,
        handler,
    )
    runtime = TelemetryRuntime(TelemetryManifest((typed.erase(),)))
    runtime.start()

    runtime.emit_sync(_Event(1))
    assert handler.values == []
    await runtime.drain()

    assert handler.values == [1]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_typed_binding_filters_both_paths_and_skips_missing_sync_handler() -> None:
    handler = _Handler()
    typed = TypedTelemetryBinding(
        TelemetrySubscriptionSpec(
            TelemetryIdentity("mote.test.typed"),
            4,
            TelemetryOverflow.DROP_NEWEST,
        ),
        _accepts_event,
        handler,
    )
    runtime = TelemetryRuntime(TelemetryManifest((typed.erase(),)))
    runtime.start()

    await runtime.emit(object())
    await runtime.emit(_Event(1))
    runtime.emit_sync(_Event(2))
    await runtime.drain()

    assert handler.values == [1]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_handler_failure_degrades_only_its_subscription() -> None:
    handler = _FailingHandler()
    runtime = TelemetryRuntime(TelemetryManifest((_binding("mote.test.failure", handler),)))
    runtime.start()
    await runtime.emit(_Event(1))
    await runtime.emit(_Event(2))
    await runtime.drain()

    snapshot = runtime.snapshots()[0]
    assert snapshot.state is TelemetryState.DEGRADED
    assert snapshot.failures == 1
    assert handler.values == [2]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_dynamic_subscription_handle_owns_unsubscribe_lifecycle() -> None:
    runtime = TelemetryRuntime(TelemetryManifest(()))
    runtime.start()
    handler = _Handler()
    handle = await runtime.subscribe(_binding("mote.test.dynamic", handler))
    await runtime.emit(_Event(1))
    await runtime.drain()

    await handle.aclose()
    await runtime.emit(_Event(2))

    assert handler.values == [1]
    assert runtime.snapshots() == ()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_subscription_can_be_registered_and_closed_before_start() -> None:
    handler = _Handler()
    runtime = TelemetryRuntime(TelemetryManifest(()))
    handle = await runtime.subscribe(_binding("mote.test.prestart", handler))
    assert len(runtime.snapshots()) == 1

    await handle.aclose()
    assert runtime.snapshots() == ()
    runtime.start()
    await runtime.emit(_Event(1))
    await runtime.drain()

    assert handler.values == []
    await runtime.aclose()


@pytest.mark.asyncio
async def test_close_leaves_no_telemetry_owner_tasks() -> None:
    runtime = TelemetryRuntime(TelemetryManifest((_binding("mote.test.close", _Handler()),)))
    runtime.start()

    await runtime.aclose()
    await asyncio.sleep(0)

    assert runtime.state is TelemetryState.CLOSED
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("telemetry-subscription:")
        and not task.done()
    ]


def test_identity_has_explicit_byte_bound() -> None:
    valid = "mote.test." + "a" * (MAX_TELEMETRY_IDENTITY_BYTES - 10)
    TelemetrySubscriptionSpec(
        identity=TelemetryIdentity(valid),
        capacity=1,
        overflow=TelemetryOverflow.DROP_NEWEST,
    )
    with pytest.raises(ValueError, match="byte bound"):
        TelemetrySubscriptionSpec(
            identity=TelemetryIdentity(valid + "a"),
            capacity=1,
            overflow=TelemetryOverflow.DROP_NEWEST,
        )


@pytest.mark.asyncio
async def test_close_drains_inflight_and_queued_events() -> None:
    handler = _BlockingHandler()
    runtime = TelemetryRuntime(TelemetryManifest((_binding("mote.test.close_race", handler),)))
    runtime.start()
    await runtime.emit(_Event(1))
    await handler.entered.wait()
    await runtime.emit(_Event(2))

    close_task = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)
    handler.release.set()
    await close_task

    assert handler.values == [1, 2]
    assert runtime.state is TelemetryState.CLOSED
