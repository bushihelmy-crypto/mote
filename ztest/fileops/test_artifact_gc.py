from __future__ import annotations

import hashlib
import multiprocessing
import queue
from contextlib import contextmanager

import pytest

from mote.contracts.fileops.models import ArtifactGarbageCollectionState, BlobRef
from mote.runtime.fileops.artifact_gc import ArtifactGarbageCollector
from mote.runtime.fileops.artifact_lifecycle import (
    ArtifactLifecycleCatalog,
    ArtifactLifecycleConflictError,
    ArtifactObjectState,
)
from mote.runtime.fileops.artifact_reachability import ArtifactReachability
from mote.runtime.fileops.artifact_repository import ArtifactReclaimStatus, ArtifactRepository
from mote.runtime.fileops.cursor_registry import ArtifactPinSnapshot

_NOW_NS = 4_000_000_000_000_000_000


class _Reachability:
    def __init__(self, artifacts=(), on_scan=None):
        self.artifacts = tuple(artifacts)
        self.on_scan = on_scan
        self.scans = 0

    def scan(self):
        self.scans += 1
        if self.on_scan is not None:
            self.on_scan()
        return ArtifactReachability(roots=(), artifacts=self.artifacts)


class _Pins:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    @contextmanager
    def freeze_pins(self):
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        yield self.snapshots[index]


def _ref(data: bytes) -> BlobRef:
    return BlobRef(hashlib.sha256(data).hexdigest(), len(data))


def _publish(repository, data, *, release=True):
    artifact = _ref(data)
    catalog = repository.catalog
    reservation = catalog.reserve(artifact.size, "test", 60)
    stage = catalog.stage(reservation, artifact.size)
    assert repository.put(stage, (data,)) == artifact
    if release:
        catalog.release(reservation)
    return artifact, reservation


def _collector(repository, reachability, pins=(), *, minimum_age_ns=10):
    snapshot = ArtifactPinSnapshot(epoch=0, revision=1, artifacts=tuple(pins))
    return ArtifactGarbageCollector(
        repository=repository,
        reachability=reachability,
        pins=_Pins((snapshot,)),
        minimum_quarantine_age_ns=minimum_age_ns,
    )


def _reclaim_worker(root, candidate, ready, start, outcomes):
    repository = ArtifactRepository(root, hard_limit_bytes=64)
    ready.put(True)
    start.wait(5)
    result = repository.reclaim(candidate)
    outcomes.put(result.status.value)


def test_generation_advances_for_publication_and_release(tmp_path):
    catalog = ArtifactLifecycleCatalog(tmp_path / "blobs", hard_limit_bytes=64)
    generations = [catalog.gc_snapshot().generation]
    artifact = _ref(b"artifact")

    reservation = catalog.reserve(artifact.size, "owner", 60)
    generations.append(catalog.gc_snapshot().generation)
    stage = catalog.stage(reservation, artifact.size)
    generations.append(catalog.gc_snapshot().generation)
    catalog.record_staged(stage, artifact)
    generations.append(catalog.gc_snapshot().generation)
    catalog.mark_live(stage, artifact)
    generations.append(catalog.gc_snapshot().generation)
    catalog.release(reservation)
    generations.append(catalog.gc_snapshot().generation)

    assert generations == list(range(6))


def test_first_pass_excludes_roots_pins_and_reservation_objects(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=128)
    catalog = repository.catalog
    rooted, _ = _publish(repository, b"rooted")
    pinned, _ = _publish(repository, b"pinned")
    unreachable, _ = _publish(repository, b"unreachable")
    reservation_owned, reservation = _publish(
        repository,
        b"reservation-owned",
        release=False,
    )
    collector = _collector(repository, _Reachability((rooted,)), (pinned,))

    report = collector.quarantine(limit=10, now_ns=_NOW_NS)

    assert tuple(item.artifact for item in report.quarantined_objects) == (unreachable,)
    assert catalog.object(rooted.digest).state == ArtifactObjectState.LIVE
    assert catalog.object(pinned.digest).state == ArtifactObjectState.LIVE
    assert catalog.object(reservation_owned.digest).state == ArtifactObjectState.LIVE
    assert catalog.reservation_objects(reservation)[0].artifact == reservation_owned


def test_quarantine_pass_is_stably_bounded(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=128)
    artifacts = tuple(_publish(repository, content)[0] for content in (b"first", b"second", b"third"))
    collector = _collector(repository, _Reachability())

    report = collector.quarantine(limit=2, now_ns=_NOW_NS)

    assert len(report.quarantined_objects) == 2
    assert tuple(item.artifact.digest for item in report.quarantined_objects) == tuple(
        sorted(artifact.digest for artifact in artifacts)[:2]
    )


def test_generation_change_aborts_scan_without_hiding_artifacts(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=64)
    catalog = repository.catalog
    artifact, _ = _publish(repository, b"candidate")
    changed = False

    def mutate_catalog():
        nonlocal changed
        if not changed:
            changed = True
            catalog.reserve(0, "concurrent-publication", 60)

    collector = _collector(repository, _Reachability(on_scan=mutate_catalog))

    with pytest.raises(ArtifactLifecycleConflictError, match="generation changed"):
        collector.quarantine(limit=10, now_ns=_NOW_NS)

    assert catalog.object(artifact.digest).state == ArtifactObjectState.LIVE


def test_pin_freeze_uses_the_root_set_after_reachability_scan(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=64)
    catalog = repository.catalog
    artifact, _ = _publish(repository, b"candidate")
    pins = _Pins((ArtifactPinSnapshot(0, 1, ()),))

    def publish_pin():
        pins.snapshots = [ArtifactPinSnapshot(0, 2, (artifact,))]

    collector = ArtifactGarbageCollector(
        repository=repository,
        reachability=_Reachability(on_scan=publish_pin),
        pins=pins,
        minimum_quarantine_age_ns=10,
    )

    result = collector.quarantine(limit=10, now_ns=_NOW_NS)

    assert result.quarantined_objects == ()
    assert catalog.object(artifact.digest).state == ArtifactObjectState.LIVE


def test_second_independent_scan_restores_newly_protected_quarantine(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=64)
    catalog = repository.catalog
    artifact, _ = _publish(repository, b"candidate")
    reachability = _Reachability()
    collector = _collector(repository, reachability)

    first = collector.quarantine(limit=10, now_ns=_NOW_NS)
    reachability.artifacts = (artifact,)
    second = collector.prepare_deletions(limit=10, now_ns=_NOW_NS + 100)

    assert first.quarantined_objects[0].artifact == artifact
    assert second.restored_objects[0].artifact == artifact
    assert second.deletion_candidates == ()
    assert reachability.scans == 2
    assert catalog.object(artifact.digest).state == ArtifactObjectState.LIVE


def test_minimum_age_precedes_durable_deleting_candidate(tmp_path):
    root = tmp_path / "blobs"
    repository = ArtifactRepository(root, hard_limit_bytes=64)
    artifact, _ = _publish(repository, b"candidate")
    collector = _collector(repository, _Reachability(), minimum_age_ns=50)

    collector.quarantine(limit=10, now_ns=_NOW_NS)
    early = collector.prepare_deletions(limit=10, now_ns=_NOW_NS + 49)
    ready = collector.prepare_deletions(limit=10, now_ns=_NOW_NS + 50)
    reopened = ArtifactLifecycleCatalog(root, hard_limit_bytes=64)

    assert early.deletion_candidates == ()
    assert ready.deletion_candidates[0].artifact == artifact
    assert reopened.object(artifact.digest).state == ArtifactObjectState.DELETING
    assert reopened.object(artifact.digest).quarantined_at_ns == _NOW_NS


def test_expedited_cycle_reclaims_after_its_independent_second_scan(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=64)
    artifact, _ = _publish(repository, b"candidate")
    collector = _collector(repository, _Reachability(), minimum_age_ns=50)

    normal = collector.run_cycle(limit=1, now_ns=_NOW_NS)
    expedited = collector.run_cycle(
        limit=1,
        expedited=True,
        now_ns=_NOW_NS,
    )

    assert normal.deletion.deletion_candidates == ()
    assert expedited.reclamation.results[0].candidate.artifact == artifact
    assert expedited.reclamation.results[0].status == ArtifactReclaimStatus.RECLAIMED
    assert repository.catalog.object(artifact.digest) is None


def test_catalog_removal_requires_exact_deleting_candidate(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=64)
    catalog = repository.catalog
    artifact, _ = _publish(repository, b"candidate")

    with pytest.raises(ArtifactLifecycleConflictError, match="not an exact deleting"):
        catalog.complete_deletion(artifact)

    collector = _collector(repository, _Reachability(), minimum_age_ns=0)
    collector.quarantine(limit=10, now_ns=_NOW_NS)
    collector.prepare_deletions(limit=10, now_ns=_NOW_NS)
    catalog.complete_deletion(artifact)

    assert catalog.object(artifact.digest) is None
    assert catalog.health().physical_bytes == 0


def test_cycle_persists_successful_reclamation_status(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=64)
    artifact, _ = _publish(repository, b"candidate")
    collector = _collector(repository, _Reachability(), minimum_age_ns=0)

    cycle = collector.run_cycle(limit=1, now_ns=_NOW_NS)
    status = repository.catalog.health().garbage_collection

    assert cycle.quarantine.quarantined_objects[0].artifact == artifact
    assert cycle.reclamation.results[0].status == ArtifactReclaimStatus.RECLAIMED
    assert status.state == ArtifactGarbageCollectionState.SUCCEEDED
    assert status.completed_at_ns == _NOW_NS
    assert status.quarantined_objects == 1
    assert status.deletion_candidates == 1
    assert status.reclaimed_objects == 1
    assert status.reclaimed_bytes == artifact.size


def test_cycle_persists_fail_closed_scan_status(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=64)

    def fail_scan():
        raise RuntimeError("injected root failure")

    collector = _collector(repository, _Reachability(on_scan=fail_scan))

    with pytest.raises(RuntimeError, match="injected root failure"):
        collector.run_cycle(limit=1, now_ns=_NOW_NS)

    status = repository.catalog.health().garbage_collection
    assert status.state == ArtifactGarbageCollectionState.FAILED
    assert status.completed_at_ns == _NOW_NS
    assert "RuntimeError" in status.failure


def test_reclamation_removes_payload_and_catalog_record(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=64)
    artifact, _ = _publish(repository, b"candidate")
    collector = _collector(repository, _Reachability(), minimum_age_ns=0)
    collector.quarantine(limit=10, now_ns=_NOW_NS)
    collector.prepare_deletions(limit=1, now_ns=_NOW_NS)
    payload = repository._payload_path(artifact)

    sweep = collector.reclaim_deleting(limit=1)

    assert sweep.candidates[0].artifact == artifact
    assert sweep.results[0].status == ArtifactReclaimStatus.RECLAIMED
    assert not payload.exists()
    assert repository.catalog.object(artifact.digest) is None


def test_reclamation_resumes_after_unlink_before_catalog_completion(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "blobs"
    repository = ArtifactRepository(root, hard_limit_bytes=64)
    artifact, _ = _publish(repository, b"candidate")
    collector = _collector(repository, _Reachability(), minimum_age_ns=0)
    collector.quarantine(limit=10, now_ns=_NOW_NS)
    candidate = collector.prepare_deletions(
        limit=1,
        now_ns=_NOW_NS,
    ).deletion_candidates[0]

    def crash_after_unlink(_artifact):
        raise RuntimeError("injected crash after payload deletion")

    monkeypatch.setattr(repository.catalog, "complete_deletion", crash_after_unlink)
    with pytest.raises(RuntimeError, match="injected crash"):
        repository.reclaim(candidate)

    assert not repository._payload_path(artifact).exists()
    assert repository.catalog.object(artifact.digest).state == ArtifactObjectState.DELETING
    reopened = ArtifactRepository(root, hard_limit_bytes=64)
    result = reopened.reclaim(candidate)
    assert result.status == ArtifactReclaimStatus.RECLAIMED
    assert reopened.catalog.object(artifact.digest) is None


def test_restarted_collector_drains_existing_deleting_candidate(tmp_path):
    root = tmp_path / "blobs"
    repository = ArtifactRepository(root, hard_limit_bytes=64)
    artifact, _ = _publish(repository, b"candidate")
    collector = _collector(repository, _Reachability(), minimum_age_ns=0)
    collector.quarantine(limit=10, now_ns=_NOW_NS)
    collector.prepare_deletions(limit=1, now_ns=_NOW_NS)

    reopened = ArtifactRepository(root, hard_limit_bytes=64)
    resumed = _collector(reopened, _Reachability(), minimum_age_ns=0)
    sweep = resumed.reclaim_deleting(limit=1)

    assert sweep.candidates[0].artifact == artifact
    assert sweep.results[0].status == ArtifactReclaimStatus.RECLAIMED


def test_stale_candidate_cannot_delete_republished_payload(tmp_path):
    repository = ArtifactRepository(tmp_path / "blobs", hard_limit_bytes=64)
    artifact, _ = _publish(repository, b"candidate")
    collector = _collector(repository, _Reachability(), minimum_age_ns=0)
    collector.quarantine(limit=10, now_ns=_NOW_NS)
    candidate = collector.prepare_deletions(
        limit=1,
        now_ns=_NOW_NS,
    ).deletion_candidates[0]
    assert repository.reclaim(candidate).status == ArtifactReclaimStatus.RECLAIMED
    republished, _ = _publish(repository, b"candidate")

    result = repository.reclaim(candidate)

    assert result.status == ArtifactReclaimStatus.SUPERSEDED
    assert repository.read_bytes(republished) == b"candidate"


def test_cross_process_reclamation_converges_idempotently(tmp_path):
    root = tmp_path / "blobs"
    repository = ArtifactRepository(root, hard_limit_bytes=64)
    _, _ = _publish(repository, b"candidate")
    collector = _collector(repository, _Reachability(), minimum_age_ns=0)
    collector.quarantine(limit=10, now_ns=_NOW_NS)
    candidate = collector.prepare_deletions(
        limit=1,
        now_ns=_NOW_NS,
    ).deletion_candidates[0]
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    outcomes = context.Queue()
    processes = tuple(
        context.Process(
            target=_reclaim_worker,
            args=(str(root), candidate, ready, start, outcomes),
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

    statuses = {outcomes.get(timeout=2) for _ in processes}
    assert statuses == {"reclaimed", "already_reclaimed"}
    assert repository.catalog.object(candidate.artifact.digest) is None


def test_verified_reader_holds_shared_lock_until_eof(tmp_path):
    root = tmp_path / "blobs"
    repository = ArtifactRepository(root, hard_limit_bytes=64)
    artifact, _ = _publish(repository, b"candidate")
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    outcomes = context.Queue()

    with repository.open_verified(artifact) as stream:
        collector = _collector(repository, _Reachability(), minimum_age_ns=0)
        collector.quarantine(limit=10, now_ns=_NOW_NS)
        candidate = collector.prepare_deletions(
            limit=1,
            now_ns=_NOW_NS,
        ).deletion_candidates[0]
        process = context.Process(
            target=_reclaim_worker,
            args=(str(root), candidate, ready, start, outcomes),
        )
        process.start()
        assert ready.get(timeout=10)
        start.set()
        with pytest.raises(queue.Empty):
            outcomes.get(timeout=0.2)
        assert stream.read() == b"candidate"
        assert stream.read() == b""

    process.join(10)
    assert process.exitcode == 0
    assert outcomes.get(timeout=2) == "reclaimed"
