from __future__ import annotations

import asyncio
import threading

import pytest

from mote.contracts.agent.cancellation import AgentCancellationDisposition, AgentCancellationReceipt
from mote.contracts.agent.capacity import CapacityReservationDisposition, LogicalCapacityReservationReceipt
from mote.contracts.agent.lineage import SpawnAdvanceDisposition, SpawnLifecycle, SpawnRequest
from mote.orchestration.agents.cancellation import SubtreeCancellationCoordinator
from mote.orchestration.agents.lineage.store import AgentLineageStore
from mote.runtime.control.leases import InMemoryLeaseCoordinator


def _capacity(identity: str):
    return LogicalCapacityReservationReceipt(identity, 1, (), CapacityReservationDisposition.RESERVED)


def _activate(store, request, lease):
    record = store.request_spawn(
        request, capacity=_capacity(request.capacity_reservation_id), budget=None, lease=lease
    ).record
    assert record is not None
    for phase, kwargs in (
        (SpawnLifecycle.ADMITTED, {}),
        (SpawnLifecycle.LINEAGE_COMMITTED, {}),
        (SpawnLifecycle.PLACEMENT_PENDING, {"placement": "worker"}),
        (SpawnLifecycle.INCARNATION_STARTED, {"placement": "worker", "incarnation_generation": 1}),
        (SpawnLifecycle.ACTIVE, {}),
    ):
        receipt = store.advance(request.request_id, phase, expected_revision=record.revision, lease=lease, **kwargs)
        assert receipt.record is not None
        record = receipt.record
    return record


class _Dispatcher:
    def __init__(self, dispositions=None, slow=()):
        self.dispositions = dispositions or {}
        self.slow = set(slow)
        self.commands = []

    async def cancel_agent_scope(self, command):
        self.commands.append(command)
        if command.target_agent_id in self.slow:
            await asyncio.sleep(1)
        return AgentCancellationReceipt(
            command.target_agent_id,
            command.cancellation_epoch,
            self.dispositions.get(command.target_agent_id, AgentCancellationDisposition.SETTLED),
        )


@pytest.mark.asyncio
async def test_subtree_cancel_aggregates_each_agent_without_reading_pool_state(tmp_path):
    leases = InMemoryLeaseCoordinator()
    lease = leases.acquire("lineage", "owner", 30)
    store = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=leases)
    store.register_root("root", "definition", lease=lease)
    child = _activate(store, SpawnRequest("child", "root", "root", "/root/child", "child", "d", "c1", ()), lease)
    assert child.logical_agent_id is not None
    grandchild = _activate(
        store, SpawnRequest("grand", "root", child.logical_agent_id, "/root/child/grand", "grand", "d", "c2", ()), lease
    )
    dispatcher = _Dispatcher({grandchild.logical_agent_id: AgentCancellationDisposition.OWNER_LOST})
    receipt = await SubtreeCancellationCoordinator(store, dispatcher).cancel(
        child.logical_agent_id, lease=lease, timeout_seconds=0.1
    )
    assert {item.target_agent_id for item in receipt.settlements} == {
        child.logical_agent_id,
        grandchild.logical_agent_id,
    }
    assert {item.disposition for item in receipt.settlements} == {
        AgentCancellationDisposition.SETTLED,
        AgentCancellationDisposition.OWNER_LOST,
    }


@pytest.mark.asyncio
async def test_retry_reuses_epoch_and_timeout_is_typed(tmp_path):
    leases = InMemoryLeaseCoordinator()
    lease = leases.acquire("lineage", "owner", 30)
    store = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=leases)
    store.register_root("root", "definition", lease=lease)
    child = _activate(store, SpawnRequest("child", "root", "root", "/root/child", None, "d", "c", ()), lease)
    assert child.logical_agent_id is not None
    dispatcher = _Dispatcher(slow={child.logical_agent_id})
    coordinator = SubtreeCancellationCoordinator(store, dispatcher)
    first = await coordinator.cancel(child.logical_agent_id, lease=lease, timeout_seconds=0.001)
    retry = await coordinator.cancel(
        child.logical_agent_id,
        lease=lease,
        timeout_seconds=0.001,
        cancellation_epoch=first.cancellation_epoch,
    )
    assert first.cancellation_epoch == retry.cancellation_epoch
    assert retry.settlements[0].disposition is AgentCancellationDisposition.TIMEOUT


def test_spawn_and_cancel_share_lineage_transaction_without_leaking_child(tmp_path):
    leases = InMemoryLeaseCoordinator()
    lease = leases.acquire("lineage", "owner", 30)
    store = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=leases)
    store.register_root("root", "definition", lease=lease)
    request = SpawnRequest("child", "root", "root", "/root/child", None, "d", "c", ())
    barrier = threading.Barrier(2)
    result = []

    def spawn():
        barrier.wait()
        result.append(store.request_spawn(request, capacity=_capacity("c"), budget=None, lease=lease))

    thread = threading.Thread(target=spawn)
    thread.start()
    barrier.wait()
    snapshot = store.begin_subtree_cancellation("root", lease=lease)
    thread.join()
    record = store.record_for_request("child")
    assert record is None or record.lifecycle is SpawnLifecycle.ABORTED
    assert "child" not in snapshot.agent_ids
    if result[0].record is None:
        assert result[0].disposition is SpawnAdvanceDisposition.CONFLICT


def test_stale_epoch_cannot_authorize_new_generation(tmp_path):
    leases = InMemoryLeaseCoordinator()
    lease = leases.acquire("lineage", "owner", 30)
    store = AgentLineageStore(tmp_path / "lineage.json", lease_coordinator=leases)
    store.register_root("root", "definition", lease=lease)
    first = store.begin_subtree_cancellation("root", lease=lease)
    second = store.begin_subtree_cancellation("root", lease=lease)
    assert second.cancellation_epoch == first.cancellation_epoch + 1
    with pytest.raises(ValueError, match="stale"):
        store.cancellation_snapshot("root", cancellation_epoch=first.cancellation_epoch)
