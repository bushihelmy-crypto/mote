from __future__ import annotations

import hashlib
import multiprocessing

import pytest

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.file.errors import SnapshotDurabilityError
from mote.runtime.fileops.mutation.artifact_catalog import (
    ArtifactLifecycleConflictError,
    ArtifactObjectState,
    ArtifactReservationState,
)
from mote.runtime.fileops.mutation.artifacts import ArtifactWriteScopeState
from mote.ztest.fileops_factory import FileMutationArtifactRepository


def _repository(tmp_path, limit=1_024) -> FileMutationArtifactRepository:
    return FileMutationArtifactRepository(tmp_path / "blobs", hard_limit_bytes=limit)


def _write_same_payload(root: str, ready, start, outcomes) -> None:
    repository = FileMutationArtifactRepository(root, hard_limit_bytes=64)
    reservation = repository.reserve(16, "worker", 60)
    stage = repository.stage(reservation, 16)
    ready.put(True)
    start.wait(5)
    artifact = repository.put(stage, (b"shared-payload",))
    outcomes.put((artifact.digest, artifact.size))


def test_staged_put_is_durable_live_and_readable_after_restart(tmp_path):
    repository = _repository(tmp_path)
    reservation = repository.reserve(32, "snapshot", 60)
    stage = repository.stage(reservation, 32)

    artifact = repository.put(stage, (b"first\n", b"second\n"))
    reopened = _repository(tmp_path)

    assert reopened.read_bytes(artifact) == b"first\nsecond\n"
    assert reopened.read_range(artifact, offset=3, limit=8) == b"st\nsecon"
    assert tuple(reopened.iter_lines(artifact)) == (b"first\n", b"second\n")
    reopened.verify(artifact)
    assert reopened.catalog.object(artifact.digest).state == ArtifactObjectState.LIVE


def test_incremental_capture_uses_the_same_bounded_publication_path(tmp_path):
    repository = _repository(tmp_path)
    reservation = repository.reserve(8, "extractor", 60)
    stage = repository.stage(reservation, 8)

    with repository.capture(stage) as capture:
        capture.write(b"abc")
        capture.write(b"def")
        assert capture.size == 6
        artifact = capture.seal()

    assert repository.read_bytes(artifact) == b"abcdef"
    assert repository.catalog.health().staging_objects == 0


def test_stream_hard_limit_stops_before_writing_the_oversized_chunk(tmp_path):
    repository = _repository(tmp_path)
    reservation = repository.reserve(5, "bounded", 60)
    stage = repository.stage(reservation, 5)
    consumed = []

    def chunks():
        for chunk in (b"abc", b"def", b"must-not-be-consumed"):
            consumed.append(chunk)
            yield chunk

    with pytest.raises(SnapshotDurabilityError, match="exceeds its stage"):
        repository.put(stage, chunks())

    assert consumed == [b"abc", b"def"]
    assert repository.catalog.health().open_stages == 0
    assert repository.catalog.reservation(reservation.reservation_id).remaining_bytes == 5
    assert tuple(repository.incoming_root.iterdir()) == ()


def test_abandoned_capture_aborts_its_explicit_stage(tmp_path):
    repository = _repository(tmp_path)
    reservation = repository.reserve(10, "cancelled", 60)
    stage = repository.stage(reservation, 10)

    with repository.capture(stage) as capture:
        capture.write(b"partial")

    assert repository.catalog.health().open_stages == 0
    assert repository.catalog.reservation(reservation.reservation_id).remaining_bytes == 10
    assert tuple(repository.incoming_root.iterdir()) == ()


def test_reads_reject_missing_and_staging_catalog_objects(tmp_path):
    repository = _repository(tmp_path)
    missing = ContentIdentity(digest=hashlib.sha256(b"missing").hexdigest(), size=7)
    with pytest.raises(SnapshotDurabilityError, match="not registered"):
        repository.read_bytes(missing)

    reservation = repository.reserve(8, "staging", 60)
    stage = repository.stage(reservation, 8)
    staging = ContentIdentity(digest=hashlib.sha256(b"staging").hexdigest(), size=7)
    repository.catalog.record_staged(stage, staging)
    with pytest.raises(SnapshotDurabilityError, match="not live"):
        repository.verify(staging)


def test_live_payload_corruption_is_never_returned(tmp_path):
    repository = _repository(tmp_path)
    reservation = repository.reserve(8, "integrity", 60)
    stage = repository.stage(reservation, 8)
    artifact = repository.put(stage, (b"original",))
    payload = repository.root / artifact.digest[:2] / artifact.digest
    payload.write_bytes(b"corrupt!")

    with pytest.raises(SnapshotDurabilityError, match="integrity"):
        repository.read_bytes(artifact)
    with pytest.raises(SnapshotDurabilityError, match="integrity"):
        repository.read_range(artifact, offset=0, limit=2)
    with pytest.raises(SnapshotDurabilityError, match="integrity"):
        repository.verify(artifact)


def test_deduplicated_put_keeps_one_physical_live_object(tmp_path):
    repository = _repository(tmp_path)
    first_reservation = repository.reserve(16, "first", 60)
    second_reservation = repository.reserve(16, "second", 60)
    first_stage = repository.stage(first_reservation, 16)
    second_stage = repository.stage(second_reservation, 16)

    first = repository.put(first_stage, (b"same",))
    second = repository.put(second_stage, (b"same",))

    assert first == second
    assert repository.catalog.health().physical_bytes == 4
    assert repository.read_bytes(first) == b"same"
    assert repository.catalog.reservation_objects(first_reservation)[0].artifact == first
    assert repository.catalog.reservation_objects(second_reservation)[0].artifact == first


def test_competing_stage_reconciles_a_durably_sealed_staging_owner(
    tmp_path,
    monkeypatch,
):
    repository = _repository(tmp_path)
    first_reservation = repository.reserve(16, "crashed-owner", 60)
    second_reservation = repository.reserve(16, "reconciler", 60)
    first_stage = repository.stage(first_reservation, 16)
    second_stage = repository.stage(second_reservation, 16)
    original_mark_live = repository.catalog.mark_live

    def crash_after_record(*args, **kwargs):
        raise RuntimeError("injected crash boundary")

    monkeypatch.setattr(repository.catalog, "mark_live", crash_after_record)
    with pytest.raises(SnapshotDurabilityError, match="publish staged"):
        repository.put(first_stage, (b"shared",))
    artifact = ContentIdentity(digest=hashlib.sha256(b"shared").hexdigest(), size=6)
    assert repository.catalog.object(artifact.digest).state == ArtifactObjectState.STAGING

    monkeypatch.setattr(repository.catalog, "mark_live", original_mark_live)
    assert repository.put(second_stage, (b"shared",)) == artifact
    assert repository.catalog.object(artifact.digest).state == ArtifactObjectState.LIVE
    assert repository.read_bytes(artifact) == b"shared"


def test_cross_process_publication_converges_on_one_verified_payload(tmp_path):
    root = tmp_path / "blobs"
    FileMutationArtifactRepository(root, hard_limit_bytes=64)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    outcomes = context.Queue()
    processes = tuple(
        context.Process(
            target=_write_same_payload,
            args=(str(root), ready, start, outcomes),
        )
        for _ in range(2)
    )
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=10)
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    references = tuple(ContentIdentity(*outcomes.get(timeout=2)) for _ in processes)
    assert references[0] == references[1]
    reopened = FileMutationArtifactRepository(root, hard_limit_bytes=64)
    assert reopened.read_bytes(references[0]) == b"shared-payload"
    assert reopened.catalog.health().physical_bytes == len(b"shared-payload")


def test_bounded_read_rejects_from_metadata_before_payload_access(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    reservation = repository.reserve(16, "bounded-read", 60)
    stage = repository.stage(reservation, 16)
    artifact = repository.put(stage, (b"larger",))
    accessed = []

    def forbidden_path(ref):
        accessed.append(ref)
        raise AssertionError("oversized artifact reached payload access")

    monkeypatch.setattr(repository, "_payload_path", forbidden_path)
    with pytest.raises(SnapshotDurabilityError, match="bounded read"):
        repository.read_bounded(artifact, maximum_bytes=3)
    assert accessed == []


def test_repository_exposes_no_unreserved_payload_convenience_api(tmp_path):
    repository = _repository(tmp_path)

    assert not hasattr(repository, "put_bytes")
    assert not hasattr(repository, "put_chunks")
    assert not hasattr(repository, "new_capture")
    assert not hasattr(repository, "path_for")
    assert not hasattr(repository, "get")


def test_write_scope_releases_only_after_explicit_complete_and_exit(tmp_path):
    repository = _repository(tmp_path)
    business_root = tmp_path / "business"
    business_root.mkdir()
    scope = repository.write_scope(
        owner="edit-plan",
        maximum_bytes=16,
        ttl_seconds=60,
    )

    with scope:
        first = scope.put_bytes(b"first")
        second = scope.put_chunks((b"sec", b"ond"), maximum_bytes=8)
        assert scope.state == ArtifactWriteScopeState.ACTIVE
        assert scope.written_bytes == 11
        assert scope.remaining_bytes == 5
        assert scope.artifacts == (first, second)
        assert repository.catalog.reservation(scope.reservation.reservation_id).state == (
            ArtifactReservationState.ACTIVE
        )
        assert len(repository.catalog.reservation_objects(scope.reservation)) == 2
        scope.complete(durability_root=business_root)
        assert scope.state == ArtifactWriteScopeState.COMPLETED
        assert len(repository.catalog.reservation_objects(scope.reservation)) == 2

    assert scope.state == ArtifactWriteScopeState.RELEASED
    assert repository.catalog.reservation(scope.reservation.reservation_id).state == (ArtifactReservationState.RELEASED)
    assert repository.catalog.reservation_objects(scope.reservation) == ()
    assert repository.read_bytes(first) == b"first"
    assert repository.read_bytes(second) == b"second"


def test_write_scope_without_complete_aborts_and_releases(tmp_path):
    repository = _repository(tmp_path)
    scope = repository.write_scope(
        owner="forgotten",
        maximum_bytes=8,
        ttl_seconds=60,
    )

    with pytest.raises(ArtifactLifecycleConflictError, match="without complete"):
        with scope:
            artifact = scope.put_bytes(b"live")

    assert scope.state == ArtifactWriteScopeState.ABORTED
    assert repository.catalog.reservation(scope.reservation.reservation_id).state == (ArtifactReservationState.RELEASED)
    assert repository.catalog.reservation_objects(scope.reservation) == ()
    assert repository.read_bytes(artifact) == b"live"


def test_write_scope_exception_aborts_open_stage_and_releases(tmp_path):
    repository = _repository(tmp_path)
    scope = repository.write_scope(
        owner="failing-stream",
        maximum_bytes=10,
        ttl_seconds=60,
    )

    def failing_chunks():
        yield b"partial"
        raise RuntimeError("stream failed")

    with pytest.raises(RuntimeError, match="stream failed"):
        with scope:
            scope.put_chunks(failing_chunks(), maximum_bytes=10)

    assert scope.state == ArtifactWriteScopeState.ABORTED
    assert repository.catalog.health().open_stages == 0
    assert repository.catalog.reservation(scope.reservation.reservation_id).state == (ArtifactReservationState.RELEASED)
    assert tuple(repository.incoming_root.iterdir()) == ()


def test_exception_after_durable_complete_releases_without_relabeling_abort(tmp_path):
    repository = _repository(tmp_path)
    business_root = tmp_path / "business"
    business_root.mkdir()
    scope = repository.write_scope(
        owner="completed-then-failed",
        maximum_bytes=8,
        ttl_seconds=60,
    )

    with pytest.raises(RuntimeError, match="downstream failed"):
        with scope:
            artifact = scope.put_bytes(b"durable")
            scope.complete(durability_root=business_root)
            raise RuntimeError("downstream failed")

    assert scope.state == ArtifactWriteScopeState.RELEASED
    assert repository.catalog.reservation(scope.reservation.reservation_id).state == (ArtifactReservationState.RELEASED)
    assert repository.read_bytes(artifact) == b"durable"


def test_write_scope_enforces_total_logical_budget_including_dedup(tmp_path):
    repository = _repository(tmp_path)
    business_root = tmp_path / "business"
    business_root.mkdir()
    seed_reservation = repository.reserve(3, "seed", 60)
    seed_stage = repository.stage(seed_reservation, 3)
    repository.put(seed_stage, (b"abc",))
    repository.release(seed_reservation)
    scope = repository.write_scope(
        owner="dedup-budget",
        maximum_bytes=5,
        ttl_seconds=60,
    )

    with scope:
        scope.put_bytes(b"abc")
        assert scope.written_bytes == 3
        with pytest.raises(SnapshotDurabilityError, match="remaining total budget"):
            scope.put_chunks((b"xyz",), maximum_bytes=3)
        scope.put_bytes(b"de")
        assert scope.written_bytes == 5
        assert scope.remaining_bytes == 0
        scope.complete(durability_root=business_root)

    assert scope.state == ArtifactWriteScopeState.RELEASED


def test_write_scope_complete_fsyncs_business_root_before_release(
    tmp_path,
    monkeypatch,
):
    repository = _repository(tmp_path)
    business_root = tmp_path / "durable-owner"
    business_root.mkdir()
    scope = repository.write_scope(
        owner="journal",
        maximum_bytes=4,
        ttl_seconds=60,
    )
    fsynced = []

    def record_fsync(path):
        fsynced.append(path)

    with scope:
        scope.put_bytes(b"data")
        monkeypatch.setattr(
            "mote.runtime.fileops.mutation.artifacts._fsync_directory",
            record_fsync,
        )
        scope.complete(durability_root=business_root)
        assert repository.catalog.reservation(scope.reservation.reservation_id).state == (
            ArtifactReservationState.ACTIVE
        )

    assert fsynced == [business_root]
    assert scope.state == ArtifactWriteScopeState.RELEASED


def test_completed_or_released_scope_cannot_accept_more_artifacts(tmp_path):
    repository = _repository(tmp_path)
    business_root = tmp_path / "business"
    business_root.mkdir()
    scope = repository.write_scope(
        owner="state-machine",
        maximum_bytes=4,
        ttl_seconds=60,
    )

    with scope:
        scope.put_bytes(b"data")
        scope.complete(durability_root=business_root)
        with pytest.raises(ArtifactLifecycleConflictError, match="not active"):
            scope.put_bytes(b"")

    with pytest.raises(ArtifactLifecycleConflictError, match="not active"):
        scope.put_bytes(b"")
