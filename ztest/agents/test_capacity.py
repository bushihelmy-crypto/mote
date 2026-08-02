from __future__ import annotations

import threading

from mote.contracts.agent.capacity import (
    CapacityReservationDisposition,
    CapacitySettlementDisposition,
    LogicalCapacityLimit,
    LogicalCapacityScope,
    LogicalCapacityScopeKind,
)
from mote.orchestration.agents.capacity import LogicalCapacityProjection


def _limit(kind: LogicalCapacityScopeKind, identity: str, maximum: int = 1):
    return LogicalCapacityLimit(LogicalCapacityScope(kind, identity), maximum)


def test_logical_capacity_is_atomic_across_application_root_subtree_and_parent():
    projection = LogicalCapacityProjection()
    limits = (
        _limit(LogicalCapacityScopeKind.APPLICATION, "app"),
        _limit(LogicalCapacityScopeKind.ROOT, "root"),
        _limit(LogicalCapacityScopeKind.SUBTREE, "/root/team"),
        _limit(LogicalCapacityScopeKind.PARENT, "/root/team"),
    )
    barrier = threading.Barrier(8)
    dispositions: list[CapacityReservationDisposition] = []

    def reserve() -> None:
        barrier.wait()
        while True:
            receipt = projection.reserve(limits, expected_revision=projection.revision)
            if receipt.disposition is not CapacityReservationDisposition.REVISION_CONFLICT:
                dispositions.append(receipt.disposition)
                return

    threads = [threading.Thread(target=reserve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert dispositions.count(CapacityReservationDisposition.RESERVED) == 1
    assert dispositions.count(CapacityReservationDisposition.REJECTED_CAPACITY) == 7
    assert all(projection.count(limit.scope) == 1 for limit in limits)


def test_logical_settlement_is_strictly_once_and_stale_revision_fails_closed():
    projection = LogicalCapacityProjection()
    limit = _limit(LogicalCapacityScopeKind.APPLICATION, "app")
    reserved = projection.reserve((limit,), expected_revision=0)
    stale = projection.settle(reserved.reservation_id, expected_revision=0)
    assert stale.disposition is CapacitySettlementDisposition.REVISION_CONFLICT
    settled = projection.settle(reserved.reservation_id, expected_revision=projection.revision)
    assert settled.disposition is CapacitySettlementDisposition.SETTLED
    duplicate = projection.settle(reserved.reservation_id, expected_revision=projection.revision)
    assert duplicate.disposition is CapacitySettlementDisposition.ALREADY_SETTLED
    assert projection.count(limit.scope) == 0


def test_logical_projection_rebuilds_from_committed_facts():
    projection = LogicalCapacityProjection()
    root = _limit(LogicalCapacityScopeKind.ROOT, "root", maximum=2)
    first = projection.reserve((root,), expected_revision=0)
    second = projection.reserve((root,), expected_revision=projection.revision)
    projection.settle(first.reservation_id, expected_revision=projection.revision)

    rebuilt = LogicalCapacityProjection.rebuild(projection.facts())
    assert rebuilt.revision == projection.revision
    assert rebuilt.count(root.scope) == 1
    settled = rebuilt.settle(second.reservation_id, expected_revision=rebuilt.revision)
    assert settled.disposition is CapacitySettlementDisposition.SETTLED


def test_logical_projection_reopens_committed_capacity(tmp_path):
    path = tmp_path / "logical-capacity.json"
    root = _limit(LogicalCapacityScopeKind.ROOT, "root", maximum=2)
    projection = LogicalCapacityProjection(path)
    reservation = projection.reserve((root,), expected_revision=0, reservation_id="child-1")

    reopened = LogicalCapacityProjection(path)
    assert reopened.revision == reservation.revision
    assert reopened.count(root.scope) == 1
    settled = reopened.settle("child-1", expected_revision=reopened.revision)
    assert settled.disposition is CapacitySettlementDisposition.SETTLED
    assert LogicalCapacityProjection(path).count(root.scope) == 0
