from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3

import pytest

from mote.contracts.fileops.models import ArtifactGarbageCollectionState, BlobRef
from mote.runtime.fileops.artifact_lifecycle import (
    ArtifactLifecycleCatalog,
    ArtifactLifecycleConflictError,
    ArtifactObjectState,
    ArtifactQuotaExceededError,
    ArtifactReservationState,
)


def _ref(data: bytes) -> BlobRef:
    return BlobRef(digest=hashlib.sha256(data).hexdigest(), size=len(data))


def _reserve_concurrently(root: str, ready, start, outcomes) -> None:
    catalog = ArtifactLifecycleCatalog(root, hard_limit_bytes=10)
    ready.put(True)
    start.wait(5)
    try:
        reservation = catalog.reserve(7, "worker", 60)
        outcomes.put(("reserved", reservation.reservation_id))
    except ArtifactQuotaExceededError:
        outcomes.put(("quota", ""))


def test_reservation_enforces_exact_hard_quota(tmp_path):
    catalog = ArtifactLifecycleCatalog(tmp_path / "blobs", hard_limit_bytes=10)

    reservation = catalog.reserve(7, "planner", 60)

    with pytest.raises(ArtifactQuotaExceededError):
        catalog.reserve(4, "search", 60)
    health = catalog.health()
    assert health.accounted_bytes == 7
    assert health.physical_bytes == 0
    assert health.reserved_bytes == 7
    assert health.staged_allocation_bytes == 0
    assert catalog.reservation(reservation.reservation_id) == reservation


def test_stage_conserves_quota_until_object_becomes_physical(tmp_path):
    catalog = ArtifactLifecycleCatalog(tmp_path / "blobs", hard_limit_bytes=10)
    reservation = catalog.reserve(10, "snapshot", 60)

    stage = catalog.stage(reservation, 8)
    staged_health = catalog.health()
    assert staged_health.reserved_bytes == 2
    assert staged_health.staged_allocation_bytes == 8
    assert staged_health.accounted_bytes == 10

    artifact = _ref(b"data")
    staged = catalog.record_staged(stage, artifact)
    assert staged.state == ArtifactObjectState.STAGING
    sealed_health = catalog.health()
    assert sealed_health.physical_bytes == 4
    assert sealed_health.reserved_bytes == 6
    assert sealed_health.staged_allocation_bytes == 0
    assert sealed_health.accounted_bytes == 10

    live = catalog.mark_live(stage, artifact)
    assert live.state == ArtifactObjectState.LIVE
    assert catalog.mark_live(stage, artifact) == live
    assert catalog.reservation_objects(reservation) == (live,)

    released = catalog.release(reservation)
    assert released.state == ArtifactReservationState.RELEASED
    assert released.remaining_bytes == 0
    assert catalog.health().accounted_bytes == artifact.size


def test_deduplication_does_not_charge_physical_bytes_twice(tmp_path):
    catalog = ArtifactLifecycleCatalog(tmp_path / "blobs", hard_limit_bytes=20)
    artifact = _ref(b"shared")
    first = catalog.reserve(artifact.size, "first", 60)
    first_stage = catalog.stage(first, artifact.size)
    catalog.record_staged(first_stage, artifact)
    catalog.mark_live(first_stage, artifact)
    catalog.release(first)

    second = catalog.reserve(artifact.size, "second", 60)
    second_stage = catalog.stage(second, artifact.size)
    existing = catalog.record_staged(second_stage, artifact)

    assert existing.state == ArtifactObjectState.LIVE
    assert catalog.health().physical_bytes == artifact.size
    assert catalog.health().reserved_bytes == artifact.size
    assert catalog.health().accounted_bytes == artifact.size * 2
    assert catalog.reservation_objects(second) == (existing,)
    catalog.release(second)
    assert catalog.health().accounted_bytes == artifact.size


def test_release_fails_while_a_stage_is_open(tmp_path):
    catalog = ArtifactLifecycleCatalog(tmp_path / "blobs", hard_limit_bytes=10)
    reservation = catalog.reserve(10, "owner", 60)
    stage = catalog.stage(reservation, 5)

    with pytest.raises(ArtifactLifecycleConflictError, match="still owns stages"):
        catalog.release(reservation)

    catalog.abort_stage(stage)
    assert catalog.release(reservation).state == ArtifactReservationState.RELEASED


def test_recovery_promotes_sealed_objects_without_payload_interpretation(tmp_path):
    root = tmp_path / "blobs"
    catalog = ArtifactLifecycleCatalog(root, hard_limit_bytes=10)
    reservation = catalog.reserve(10, "capture", 60)
    stage = catalog.stage(reservation, 10)
    artifact = _ref(b"sealed")
    catalog.record_staged(stage, artifact)

    reopened = ArtifactLifecycleCatalog(root, hard_limit_bytes=10)

    assert reopened.object(artifact.digest).state == ArtifactObjectState.LIVE
    assert reopened.health().staging_objects == 0
    assert reopened.reservation_objects(reservation)[0].artifact == artifact


def test_recovery_only_abandons_open_stages_after_ttl(tmp_path):
    root = tmp_path / "blobs"
    catalog = ArtifactLifecycleCatalog(root, hard_limit_bytes=10)
    reservation = catalog.reserve(10, "capture", 60)
    catalog.stage(reservation, 8)

    active_report = catalog.recover(now_ns=reservation.expires_at_ns - 1)
    assert active_report.abandoned_stages == 0
    assert catalog.health().open_stages == 1

    expired_report = catalog.recover(now_ns=reservation.expires_at_ns)
    assert expired_report.abandoned_stages == 1
    assert expired_report.expired_reservations == 1
    durable = catalog.reservation(reservation.reservation_id)
    assert durable.state == ArtifactReservationState.EXPIRED
    assert durable.remaining_bytes == 0
    assert catalog.health().accounted_bytes == 0


def test_expiration_releases_completed_stage_ownership(tmp_path):
    catalog = ArtifactLifecycleCatalog(tmp_path / "blobs", hard_limit_bytes=10)
    reservation = catalog.reserve(10, "capture", 60)
    stage = catalog.stage(reservation, 10)
    artifact = _ref(b"durable")
    catalog.record_staged(stage, artifact)
    catalog.mark_live(stage, artifact)

    report = catalog.recover(now_ns=reservation.expires_at_ns)

    assert report.expired_reservations == 1
    assert catalog.reservation_objects(reservation) == ()
    assert catalog.object(artifact.digest).state == ArtifactObjectState.LIVE
    connection = sqlite3.connect(catalog.path)
    assert connection.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 0
    connection.close()


def test_logical_clock_never_moves_backwards(tmp_path):
    catalog = ArtifactLifecycleCatalog(tmp_path / "blobs", hard_limit_bytes=10)
    reservation = catalog.reserve(1, "owner", 1)

    renewed = catalog.renew(
        reservation,
        1,
        now_ns=reservation.expires_at_ns - 1,
    )

    assert renewed.expires_at_ns > reservation.expires_at_ns


def test_schema_and_limit_are_fail_closed_on_reopen(tmp_path):
    root = tmp_path / "blobs"
    catalog = ArtifactLifecycleCatalog(root, hard_limit_bytes=10)
    connection = sqlite3.connect(catalog.path)
    connection.execute("CREATE TABLE unexpected (value TEXT) STRICT")
    connection.commit()
    connection.close()

    with pytest.raises(ArtifactLifecycleConflictError, match="not canonical"):
        ArtifactLifecycleCatalog(root, hard_limit_bytes=10)

    other = tmp_path / "other"
    ArtifactLifecycleCatalog(other, hard_limit_bytes=10)
    with pytest.raises(ArtifactLifecycleConflictError, match="hard limit"):
        ArtifactLifecycleCatalog(other, hard_limit_bytes=11)


def test_garbage_collection_status_is_exact_and_durable(tmp_path):
    root = tmp_path / "blobs"
    catalog = ArtifactLifecycleCatalog(root, hard_limit_bytes=64)

    assert catalog.health().garbage_collection.state == (ArtifactGarbageCollectionState.NEVER_RUN)
    recorded = catalog.record_garbage_collection_success(
        quarantined_objects=3,
        restored_objects=2,
        deletion_candidates=1,
        reclaimed_objects=1,
        reclaimed_bytes=7,
        now_ns=123,
    )
    reopened = ArtifactLifecycleCatalog(root, hard_limit_bytes=64)

    assert reopened.health().garbage_collection == recorded
    assert recorded.state == ArtifactGarbageCollectionState.SUCCEEDED
    assert recorded.completed_at_ns is not None
    assert recorded.reclaimed_bytes == 7

    failed = reopened.record_garbage_collection_failure("injected", now_ns=124)
    assert (
        ArtifactLifecycleCatalog(
            root,
            hard_limit_bytes=64,
        )
        .health()
        .garbage_collection
        == failed
    )
    assert failed.state == ArtifactGarbageCollectionState.FAILED
    assert failed.completed_at_ns >= recorded.completed_at_ns


def test_begin_immediate_serializes_cross_process_admission(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    root = tmp_path / "blobs"
    ArtifactLifecycleCatalog(root, hard_limit_bytes=10)
    ready = ctx.Queue()
    outcomes = ctx.Queue()
    start = ctx.Event()
    processes = [
        ctx.Process(
            target=_reserve_concurrently,
            args=(str(root), ready, start, outcomes),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=20) is True
    start.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    results = sorted(outcomes.get(timeout=20)[0] for _ in processes)
    assert results == ["quota", "reserved"]


@pytest.mark.parametrize("value", [True, -1, 1.5])
def test_capacity_rejects_non_exact_integers(tmp_path, value):
    catalog = ArtifactLifecycleCatalog(tmp_path / "blobs", hard_limit_bytes=10)

    with pytest.raises(ValueError):
        catalog.reserve(value, "owner", 60)
