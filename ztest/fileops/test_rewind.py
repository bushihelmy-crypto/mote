from __future__ import annotations

import multiprocessing
import os
import shutil

import pytest

from mote.contracts.events.file.facts import RewindCommittedEvent, RewindPreparedEvent
from mote.contracts.file import (
    ByteReadRequest,
    ByteViewMode,
    ContinueReadRequest,
    ReadCursorError,
    RecoveryInDoubtError,
    TransactionStatus,
)
from mote.runtime.fileops import (
    DurableFileOperationsJournal,
    HierarchicalLockManager,
    ProjectOperationControl,
    RewindCoordinator,
    WorktreeCheckpointStore,
    project_identity,
)
from mote.runtime.fileops.cursor_registry import DurableCursorRegistry
from mote.runtime.fileops.facade import FileOperations as RuntimeFileOperations
from mote.runtime.fileops.transactions import ScopedMutationArtifacts
from mote.runtime.session.checkpoint import list_checkpoints
from mote.runtime.session.log import SessionLog
from mote.ztest.fileops_factory import FileOperations

git_required = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available",
)


def _operations(root, work, session_id="session") -> RuntimeFileOperations:
    return FileOperations(
        session_id=session_id,
        journal_path=root / session_id / "rollout.jsonl",
        get_project_root=lambda: str(work),
        lock_root=root / "locks",
    )


class _CrashJournal(DurableFileOperationsJournal):
    def __init__(self, *args, crash_stage: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._crash_stage = crash_stage

    def append(self, event):
        if self._crash_stage == "before_prepared" and isinstance(event, RewindPreparedEvent):
            os._exit(90)
        if self._crash_stage == "before_committed" and isinstance(event, RewindCommittedEvent):
            os._exit(92)
        super().append(event)
        if self._crash_stage == "after_committed" and isinstance(event, RewindCommittedEvent):
            os._exit(93)
        if self._crash_stage == "after_prepared" and isinstance(event, RewindPreparedEvent):
            os._exit(91)


def _crash_rewind(root_text: str, work_text: str, target: str, stage: str) -> None:
    from pathlib import Path

    root = Path(root_text)
    work = Path(work_text)
    locks = HierarchicalLockManager(root / "locks")
    journal = _CrashJournal(
        root / "session" / "rollout.jsonl",
        session_id="session",
        locks=locks,
        crash_stage=stage,
    )
    control = ProjectOperationControl(locks)
    coordinator = RewindCoordinator(
        session_id="session",
        git_dir=root / "session" / "git",
        locks=locks,
        journal=journal,
        control=control,
        timeline=DurableCursorRegistry(root / "session" / "cursor-registry.sqlite3"),
    )
    control.register(coordinator)
    coordinator.rewind(
        working_dir=str(work),
        target_commit=target,
        parent_commit=target,
        prompt_index=0,
    )


def _pause_rewind(
    root_text: str,
    work_text: str,
    target: str,
    entered,
    release,
    outcomes,
) -> None:
    from pathlib import Path

    import mote.runtime.fileops.rewind as rewind_module

    root = Path(root_text)
    work = Path(work_text)
    base_store = rewind_module.WorktreeCheckpointStore

    class PausingStore(base_store):
        def restore(self, commit: str) -> None:
            entered.set()
            release.wait(5)
            super().restore(commit)

    rewind_module.WorktreeCheckpointStore = PausingStore
    operations = _operations(root, work)
    try:
        result = operations.rewind(
            working_dir=str(work),
            target_commit=target,
            parent_commit=target,
            prompt_index=0,
        )
        outcomes.put(("rewind", result.status.value))
    except Exception as exc:
        outcomes.put(("rewind", type(exc).__name__))


def _compete_mutation(root_text: str, work_text: str, snapshot, outcomes) -> None:
    from pathlib import Path

    root = Path(root_text)
    work = Path(work_text)
    operations = _operations(root, work, session_id="foreign")
    try:
        with operations.artifacts.write_scope(
            owner="test-rewind-competing-mutation",
            maximum_bytes=len(b"mutation\n"),
            ttl_seconds=60,
        ) as scope:
            mutation = operations.mutation_factory.replacement(
                snapshot,
                b"mutation\n",
                scope=scope,
            )
            result = operations.mutations.commit(
                operations.mutation_factory.mutation_set(
                    source="Edit",
                    mutations=(mutation,),
                ),
                ScopedMutationArtifacts(scope),
            )
        outcomes.put(("mutation", result.status.value))
    except Exception as exc:
        outcomes.put(("mutation", type(exc).__name__))


def _setup_tree(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    target_file = work / "target.txt"
    target_file.write_text("target\n", encoding="utf-8")
    store = WorktreeCheckpointStore(tmp_path / "session" / "git", work)
    target_commit = store.capture(message="target")
    target_file.write_text("current\n", encoding="utf-8")
    return work, target_file, target_commit, store


@git_required
def test_rewind_is_committed_durable_and_invalidates_observed_snapshots(tmp_path):
    work, target_file, target_commit, store = _setup_tree(tmp_path)
    operations = _operations(tmp_path, work)
    observed, _ = operations.capture(str(target_file))
    operations.observe(observed)
    cursor = operations.read_view(
        str(target_file),
        ByteReadRequest(mode=ByteViewMode.RAW, limit=1),
    ).next_cursor

    result = operations.rewind(
        working_dir=str(work),
        target_commit=target_commit,
        parent_commit=target_commit,
        prompt_index=0,
    )

    assert result.status == TransactionStatus.COMMITTED
    assert target_file.read_text(encoding="utf-8") == "target\n"
    assert operations.observed(str(target_file)) is None
    assert operations.journal.timeline_epoch() == 1
    assert operations.cursor_registry.current_epoch == 1
    with pytest.raises(ReadCursorError):
        operations.read_view(
            str(target_file),
            ContinueReadRequest(cursor=cursor),
        )
    reopened = _operations(tmp_path, work)
    with pytest.raises(ReadCursorError):
        reopened.read_view(
            str(target_file),
            ContinueReadRequest(cursor=cursor),
        )
    assert operations.journal.rewind(result.transaction_id).status == TransactionStatus.COMMITTED
    checkpoints = list_checkpoints(SessionLog("session", base_dir=str(tmp_path)))
    assert len(checkpoints) == 1
    assert checkpoints[0].commit == result.safety_commit
    store.restore(result.safety_commit)
    assert target_file.read_text(encoding="utf-8") == "current\n"


@git_required
@pytest.mark.parametrize(
    ("stage", "exitcode", "status", "content", "checkpoint_count"),
    [
        ("after_prepared", 91, TransactionStatus.ABORTED, "current\n", 0),
        ("before_committed", 92, TransactionStatus.COMMITTED, "target\n", 1),
    ],
)
def test_rewind_process_crash_reconciles_once(
    tmp_path,
    stage,
    exitcode,
    status,
    content,
    checkpoint_count,
):
    work, target_file, target_commit, _ = _setup_tree(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_rewind,
        args=(str(tmp_path), str(work), target_commit, stage),
    )
    process.start()
    process.join(10)
    assert process.exitcode == exitcode

    operations = _operations(tmp_path, work)
    operations.reconcile()

    assert operations.journal.rewind_records()[0].status == status
    assert target_file.read_text(encoding="utf-8") == content
    operations.reconcile()
    assert len(operations.journal.rewind_records()) == 1
    assert len(list_checkpoints(SessionLog("session", base_dir=str(tmp_path)))) == checkpoint_count


@git_required
def test_rewind_recovery_marks_external_tree_in_doubt_and_blocks_mutation(tmp_path):
    work, target_file, target_commit, _ = _setup_tree(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_rewind,
        args=(str(tmp_path), str(work), target_commit, "after_prepared"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 91
    target_file.write_text("external\n", encoding="utf-8")

    owner = _operations(tmp_path, work)
    with pytest.raises(RecoveryInDoubtError):
        owner.reconcile()

    result = owner.journal.rewind_records()[0]
    assert result.status == TransactionStatus.IN_DOUBT
    assert target_file.read_text(encoding="utf-8") == "external\n"
    foreign = _operations(tmp_path, work, session_id="foreign")
    health = foreign.health()
    assert health.in_doubt_transactions == (result.transaction_id,)
    assert health.affected_paths == (str(work),)
    with pytest.raises(RecoveryInDoubtError):
        foreign.capture(str(target_file))


@git_required
def test_foreign_session_reconciles_abandoned_project_fence(tmp_path):
    work, target_file, target_commit, _ = _setup_tree(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_rewind,
        args=(str(tmp_path), str(work), target_commit, "after_prepared"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 91

    foreign = _operations(tmp_path, work, session_id="foreign")
    snapshot, raw = foreign.capture(str(target_file))

    assert raw == b"current\n"
    assert snapshot.version.digest
    assert foreign.control.records(project_identity(work)) == ()
    owner = _operations(tmp_path, work)
    assert owner.journal.rewind_records()[0].status == TransactionStatus.ABORTED


@git_required
def test_fence_without_prepared_event_is_safely_cleared(tmp_path):
    work, target_file, target_commit, _ = _setup_tree(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_rewind,
        args=(str(tmp_path), str(work), target_commit, "before_prepared"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 90

    foreign = _operations(tmp_path, work, session_id="foreign")
    _, raw = foreign.capture(str(target_file))

    assert raw == b"current\n"
    assert foreign.control.records(project_identity(work)) == ()
    assert _operations(tmp_path, work).journal.rewind_records() == ()


@git_required
def test_committed_rewind_fence_is_cleared_by_foreign_session(tmp_path):
    work, target_file, target_commit, _ = _setup_tree(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_rewind,
        args=(str(tmp_path), str(work), target_commit, "after_committed"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 93

    foreign = _operations(tmp_path, work, session_id="foreign")
    _, raw = foreign.capture(str(target_file))

    assert raw == b"target\n"
    assert foreign.control.records(project_identity(work)) == ()
    owner = _operations(tmp_path, work)
    assert owner.journal.rewind_records()[0].status == TransactionStatus.COMMITTED
    assert len(list_checkpoints(SessionLog("session", base_dir=str(tmp_path)))) == 1


@git_required
def test_rewind_exclusive_barrier_prevents_mutation_interleaving(tmp_path):
    work, target_file, target_commit, _ = _setup_tree(tmp_path)
    snapshot, _ = _operations(tmp_path, work, session_id="foreign").capture(str(target_file))
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    outcomes = context.Queue()
    rewind_process = context.Process(
        target=_pause_rewind,
        args=(
            str(tmp_path),
            str(work),
            target_commit,
            entered,
            release,
            outcomes,
        ),
    )
    rewind_process.start()
    assert entered.wait(30)
    mutation_process = context.Process(
        target=_compete_mutation,
        args=(str(tmp_path), str(work), snapshot, outcomes),
    )
    mutation_process.start()
    mutation_process.join(0.3)
    assert mutation_process.is_alive()

    release.set()
    rewind_process.join(10)
    mutation_process.join(10)
    assert rewind_process.exitcode == 0
    assert mutation_process.exitcode == 0
    results = dict(outcomes.get(timeout=2) for _ in range(2))
    assert results == {
        "rewind": TransactionStatus.COMMITTED.value,
        "mutation": "StaleSnapshotError",
    }
    assert target_file.read_text(encoding="utf-8") == "target\n"
