from __future__ import annotations

import pytest

from mote.contracts.fileops import (
    ArtifactGarbageCollectionState,
    ByteReadRequest,
    ByteViewMode,
    ContentChangedError,
    JournalDurabilityError,
    StaleSnapshotError,
)
from mote.runtime.fileops import FileOperations
from mote.runtime.fileops.artifact_lifecycle import ArtifactObjectState
from mote.runtime.fileops.transactions import ScopedMutationArtifacts


def _operations(tmp_path):
    return FileOperations(
        session_id="health",
        journal_path=tmp_path / "session" / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=tmp_path / "locks",
    )


def test_health_reports_ready_storage_and_lock_backend(tmp_path):
    operations = _operations(tmp_path)

    health = operations.health()

    assert health.ready
    assert health.lock_backend in {"posix-flock", "windows-lockfileex"}
    assert health.recovery_backlog == 0
    assert health.in_doubt_transactions == ()
    assert health.cursor_registry_readable
    assert health.timeline_epoch == 0
    assert health.artifact_catalog_readable
    assert health.artifact_hard_limit_bytes > 0
    assert health.artifact_quota_pressure == 0.0


def test_health_projects_durable_cursor_pins(tmp_path):
    target = tmp_path / "paged.bin"
    target.write_bytes(b"01234567")
    operations = _operations(tmp_path)
    page = operations.read_view(
        str(target),
        ByteReadRequest(mode=ByteViewMode.RAW, limit=4),
    )

    health = operations.health()

    assert page.next_cursor is not None
    assert health.active_cursor_leases == 1
    assert health.expired_cursor_leases == 0
    assert health.pinned_artifacts >= 2
    assert health.pinned_bytes >= len(b"01234567")
    assert health.nearest_cursor_expiry_ns is not None
    assert health.observed_snapshots == 1
    assert health.artifact_physical_bytes >= len(b"01234567")
    assert health.artifact_active_reservations == 0
    assert health.artifact_open_stages == 0


def test_health_projects_durable_garbage_collection_state(tmp_path):
    operations = _operations(tmp_path)
    with operations.artifacts.write_scope(
        owner="health-unrooted",
        maximum_bytes=7,
        ttl_seconds=60,
    ) as scope:
        artifact = scope.put_bytes(b"garbage")
        scope.discard()

    operations.collect_artifacts(limit=1)
    health = operations.health()

    assert health.ready
    assert health.artifact_gc_state == ArtifactGarbageCollectionState.SUCCEEDED
    assert health.artifact_gc_completed_at_ns is not None
    assert health.artifact_gc_quarantined_objects == 1
    assert health.artifact_quarantined_objects == 1
    assert operations.artifacts.catalog.object(artifact.digest).state == (ArtifactObjectState.QUARANTINED)

    reopened = _operations(tmp_path)
    assert reopened.health().artifact_gc_state == (ArtifactGarbageCollectionState.SUCCEEDED)
    assert reopened.health().artifact_quarantined_objects == 1


def test_health_fails_closed_after_garbage_collection_failure(tmp_path):
    operations = _operations(tmp_path)
    operations.artifacts.catalog.record_garbage_collection_failure("injected")

    health = operations.health()

    assert not health.ready
    assert health.artifact_gc_state == ArtifactGarbageCollectionState.FAILED
    assert health.artifact_gc_failure == "injected"


def test_health_reports_in_doubt_target(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    operations = _operations(tmp_path)
    snapshot, _ = operations.capture(str(target))
    original_verify = operations.mutations.publisher._verify_expected

    def external_write_before_verify(path, expected):
        target.write_bytes(b"external")
        return original_verify(path, expected)

    monkeypatch.setattr(
        operations.mutations.publisher,
        "_verify_expected",
        external_write_before_verify,
    )
    with pytest.raises((ContentChangedError, StaleSnapshotError)):
        with operations.artifacts.write_scope(
            owner="test-health-b1",
            maximum_bytes=len(b"after"),
            ttl_seconds=60,
        ) as scope:
            mutation = operations.mutation_factory.replacement(
                snapshot,
                b"after",
                scope=scope,
            )
            operations.mutations.commit(
                operations.mutation_factory.mutation_set(
                    source="Edit",
                    mutations=(mutation,),
                ),
                ScopedMutationArtifacts(scope),
            )

    health = operations.health()

    assert not health.ready
    assert len(health.in_doubt_transactions) == 1
    assert health.affected_paths == (str(target),)


def test_health_reports_unreadable_journal_without_raising(tmp_path, monkeypatch):
    operations = _operations(tmp_path)

    def fail_records():
        raise JournalDurabilityError("injected")

    monkeypatch.setattr(operations.journal, "records", fail_records)
    health = operations.health()

    assert not health.journal_readable
    assert not health.ready
