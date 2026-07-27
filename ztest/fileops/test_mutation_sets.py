from __future__ import annotations

from contextlib import contextmanager

import pytest

from mote.contracts.fileops import MutationSet, RecoveryPolicy, ReplaceMutation, TransactionStatus
from mote.contracts.fileops.errors import FilePublishError
from mote.runtime.fileops.artifact_budgets import snapshot_budget
from mote.runtime.fileops.facade import FileOperations
from mote.runtime.fileops.identity import project_identity
from mote.runtime.fileops.transactions import ScopedMutationArtifacts


def _operations(tmp_path) -> FileOperations:
    return FileOperations(
        session_id="session",
        journal_path=tmp_path / "session" / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=tmp_path / "locks",
    )


def _capture(operations, path, project_root):
    with operations.artifacts.write_scope(
        owner="test-mutation-set-snapshot",
        maximum_bytes=snapshot_budget(path.stat().st_size),
        ttl_seconds=60,
    ) as scope:
        snapshot = operations.reader.open_snapshot(
            path,
            scope=scope,
            project_root=project_root,
        )
        operations.cursor_registry.observe(
            snapshot,
            expected_epoch=operations.cursor_registry.current_epoch,
        )
        scope.complete(durability_root=operations.cursor_registry.path.parent)
        return snapshot


@contextmanager
def _replacement_set(operations, snapshots, replacements):
    with operations.artifacts.write_scope(
        owner="test-mutation-set-b1",
        maximum_bytes=sum(len(replacement) for replacement in replacements),
        ttl_seconds=60,
    ) as scope:
        mutation_set = MutationSet(
            transaction_id="multi-transaction",
            session_id="session",
            source="EditPlanner",
            recovery_policy=RecoveryPolicy.ROLLBACK_INCOMPLETE,
            mutations=tuple(
                ReplaceMutation(
                    before=snapshot,
                    after=scope.put_bytes(replacement),
                )
                for snapshot, replacement in zip(snapshots, replacements)
            ),
        )
        yield mutation_set, ScopedMutationArtifacts(scope)


def test_multi_project_mutation_set_commits_one_canonical_vector(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = first_root / "a.txt"
    second = second_root / "b.txt"
    first.write_bytes(b"before-a")
    second.write_bytes(b"before-b")
    operations = _operations(tmp_path)
    snapshots = (
        _capture(operations, first, first_root),
        _capture(operations, second, second_root),
    )
    with _replacement_set(
        operations,
        tuple(reversed(snapshots)),
        (b"after-b", b"after-a"),
    ) as (mutation_set, ownership):
        result = operations.mutations.commit(mutation_set, ownership)

    assert result.status == TransactionStatus.COMMITTED
    assert first.read_bytes() == b"after-a"
    assert second.read_bytes() == b"after-b"
    assert operations.journal.records()[0].mutation_set == mutation_set
    assert operations.control.reservations(project_identity(first_root)) == ()
    assert operations.control.reservations(project_identity(second_root)) == ()


def test_partial_multi_project_publish_is_compensated_to_b0(tmp_path, monkeypatch):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = first_root / "a.txt"
    second = second_root / "b.txt"
    first.write_bytes(b"before-a")
    second.write_bytes(b"before-b")
    operations = _operations(tmp_path)
    snapshots = (
        _capture(operations, first, first_root),
        _capture(operations, second, second_root),
    )
    original = operations.mutations.publisher.replace_from_blob
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FilePublishError("injected second publication failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        operations.mutations.publisher,
        "replace_from_blob",
        fail_second,
    )

    with _replacement_set(
        operations,
        snapshots,
        (b"after-a", b"after-b"),
    ) as (mutation_set, ownership):
        with pytest.raises(FilePublishError):
            operations.mutations.commit(mutation_set, ownership)

    assert first.read_bytes() == b"before-a"
    assert second.read_bytes() == b"before-b"
    assert operations.journal.records()[0].status == TransactionStatus.ABORTED
    assert operations.control.reservations(project_identity(first_root)) == ()
    assert operations.control.reservations(project_identity(second_root)) == ()


def test_compensation_can_resume_after_another_failure(tmp_path, monkeypatch):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = first_root / "a.txt"
    second = second_root / "b.txt"
    first.write_bytes(b"before-a")
    second.write_bytes(b"before-b")
    operations = _operations(tmp_path)
    snapshots = (
        _capture(operations, first, first_root),
        _capture(operations, second, second_root),
    )
    original = operations.mutations.publisher.replace_from_blob
    calls = 0

    def fail_publish_and_compensation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise FilePublishError("injected resumable failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        operations.mutations.publisher,
        "replace_from_blob",
        fail_publish_and_compensation,
    )
    with _replacement_set(
        operations,
        snapshots,
        (b"after-a", b"after-b"),
    ) as (mutation_set, ownership):
        with pytest.raises(FilePublishError):
            operations.mutations.commit(mutation_set, ownership)
    assert operations.journal.records()[0].status == TransactionStatus.PREPARED
    assert len(operations.control.reservations(project_identity(first_root))) == 1
    assert len(operations.control.reservations(project_identity(second_root))) == 1

    monkeypatch.setattr(
        operations.mutations.publisher,
        "replace_from_blob",
        original,
    )
    operations.control.reconcile(
        project_identity(first_root),
        label=str(first_root),
    )

    assert first.read_bytes() == b"before-a"
    assert second.read_bytes() == b"before-b"
    assert operations.journal.records()[0].status == TransactionStatus.ABORTED
    assert operations.control.reservations(project_identity(first_root)) == ()
    assert operations.control.reservations(project_identity(second_root)) == ()
