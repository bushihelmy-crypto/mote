from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from mote.orchestration.agents.identity.path import AgentPath
from mote.orchestration.agents.identity.registry import (
    AgentMetadata,
    AgentRegistry,
    IdentityReclaimDisposition,
    IdentityReservationSnapshot,
    IdentityRetentionRelease,
    SpawnReservation,
)
from mote.runtime.control.leases import InMemoryLeaseCoordinator


def _lease():
    coordinator = InMemoryLeaseCoordinator(clock=lambda: 1.0)
    return coordinator, coordinator.acquire("agent-identities", "reconciler-1", 30)


def test_fenced_reconciler_reclaims_only_exact_aborted_reservation() -> None:
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_identity()
    nickname = reservation.reserve_agent_nickname_with_preference(["worker"])
    path = AgentPath.from_string("/root/worker")
    reservation.reserve_agent_path(path)
    snapshot = reservation.snapshot()
    coordinator, lease = _lease()

    receipt = registry.reclaim_aborted_reservation(snapshot, lease=lease, coordinator=coordinator)
    assert receipt.disposition is IdentityReclaimDisposition.RECLAIMED
    assert nickname not in registry._nickname_claims
    assert path.as_str() not in registry._path_claims
    reservation.rollback()
    registry.reserve_spawn_identity().rollback()


def test_reconciler_cannot_omit_one_claim_from_reservation_snapshot() -> None:
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_identity()
    nickname = reservation.reserve_agent_nickname_with_preference(["worker"])
    path = AgentPath.from_string("/root/worker")
    reservation.reserve_agent_path(path)
    snapshot = reservation.snapshot()
    incomplete = IdentityReservationSnapshot(
        snapshot.reservation_id,
        snapshot.path,
        snapshot.path_revision,
        None,
        None,
    )
    coordinator, lease = _lease()
    receipt = registry.reclaim_aborted_reservation(incomplete, lease=lease, coordinator=coordinator)
    assert receipt.disposition is IdentityReclaimDisposition.REVISION_CONFLICT
    assert nickname in registry._nickname_claims
    assert path.as_str() in registry._path_claims
    reservation.rollback()


def test_old_or_wrong_fence_cannot_reclaim_live_reservation() -> None:
    now = [1.0]
    coordinator = InMemoryLeaseCoordinator(clock=lambda: now[0])
    stale = coordinator.acquire("agent-identities", "reconciler-1", 1)
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_identity()
    nickname = reservation.reserve_agent_nickname_with_preference(["worker"])
    snapshot = reservation.snapshot()
    now[0] = 3.0
    coordinator.acquire("agent-identities", "reconciler-2", 30)

    receipt = registry.reclaim_aborted_reservation(snapshot, lease=stale, coordinator=coordinator)
    assert receipt.disposition is IdentityReclaimDisposition.STALE_FENCE
    assert registry._nickname_claims[nickname].reservation_id == reservation.reservation_id
    reservation.rollback()


def test_committed_identity_cannot_be_reclaimed_by_aborted_snapshot() -> None:
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_identity()
    nickname = reservation.reserve_agent_nickname_with_preference(["worker"])
    path = AgentPath.from_string("/root/worker")
    reservation.reserve_agent_path(path)
    snapshot = reservation.snapshot()
    reservation.commit(AgentMetadata("agent-1", path, nickname))
    coordinator, lease = _lease()

    receipt = registry.reclaim_aborted_reservation(snapshot, lease=lease, coordinator=coordinator)
    assert receipt.disposition is IdentityReclaimDisposition.NOT_FOUND
    assert registry.agent_id_for_path(path) == "agent-1"


def test_retention_release_is_revision_bound_and_new_claim_is_aba_safe() -> None:
    registry = AgentRegistry()
    first = registry.reserve_spawn_identity()
    nickname = first.reserve_agent_nickname_with_preference(["worker"])
    path = AgentPath.from_string("/root/worker")
    first.reserve_agent_path(path)
    first.commit(AgentMetadata("agent-1", path, nickname))
    old_path_reference = registry.path_reference(path)
    old_nickname_reference = registry.nickname_reference(nickname)
    assert old_path_reference is not None
    assert old_nickname_reference is not None
    registry.release_spawned_agent("agent-1")
    path_claim = registry._path_claims[path.as_str()]
    nickname_claim = registry._nickname_claims[nickname]
    release = IdentityRetentionRelease("agent-1", path.as_str(), path_claim.revision, nickname, nickname_claim.revision)
    coordinator, lease = _lease()
    assert (
        registry.release_retained_indices(release, lease=lease, coordinator=coordinator).disposition
        is IdentityReclaimDisposition.RECLAIMED
    )

    second = registry.reserve_spawn_identity()
    second_name = second.reserve_agent_nickname_with_preference([], preferred=nickname)
    second.reserve_agent_path(path)
    new_path_revision = registry._path_claims[path.as_str()].revision
    second.commit(AgentMetadata("agent-2", path, second_name))
    stale = registry.release_retained_indices(release, lease=lease, coordinator=coordinator)
    assert stale.disposition is IdentityReclaimDisposition.REVISION_CONFLICT
    assert registry.agent_id_for_path(path) == "agent-2"
    assert new_path_revision > path_claim.revision
    assert registry.resolve_index_reference(old_path_reference) is None
    assert registry.resolve_index_reference(old_nickname_reference) is None


def test_logical_agent_identity_is_never_reused_after_terminal_release() -> None:
    registry = AgentRegistry()
    first = registry.reserve_spawn_identity()
    first.commit(AgentMetadata(agent_id="agent-1"))
    registry.release_spawned_agent("agent-1")
    second = registry.reserve_spawn_identity()
    try:
        second.commit(AgentMetadata(agent_id="agent-1"))
    except ValueError as error:
        assert "cannot be reused" in str(error)
    else:
        raise AssertionError("logical Agent identity reuse was accepted")
    second.rollback()


def test_concurrent_nickname_reservations_are_unique_without_pool_clear() -> None:
    registry = AgentRegistry()

    def reserve(_: int) -> tuple[SpawnReservation, str]:
        reservation = registry.reserve_spawn_identity()
        return reservation, reservation.reserve_agent_nickname_with_preference(["worker"])

    with ThreadPoolExecutor(max_workers=6) as executor:
        reservations = tuple(executor.map(reserve, range(6)))
    names = tuple(name for _, name in reservations)
    assert len(names) == len(set(names)) == 6
    assert set(names) == {
        "worker",
        "worker the 2nd",
        "worker the 3rd",
        "worker the 4th",
        "worker the 5th",
        "worker the 6th",
    }
    for reservation, _ in reservations:
        reservation.rollback()
