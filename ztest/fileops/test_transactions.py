from __future__ import annotations

import codecs
import multiprocessing
import os
from pathlib import Path

import pytest

from mote.contracts.conversation import UserMessage
from mote.contracts.events.file.facts import FileTransactionCommittedEvent, FileTransactionPreparedEvent
from mote.contracts.file import (
    ContentChangedError,
    EncodingRejectedError,
    FilePublishError,
    JournalDurabilityError,
    RecoveryInDoubtError,
    StaleSnapshotError,
    TransactionStatus,
)
from mote.runtime.fileops import (
    AtomicPublisher,
    DurableFileOperationsJournal,
    HierarchicalLockManager,
    MutationCoordinator,
    ProjectOperationControl,
    SealedSnapshotReader,
    project_identity,
)
from mote.runtime.fileops.metadata_manifest import PreservedMetadata, encode_metadata_manifest
from mote.runtime.fileops.mutation_factory import MutationFactory
from mote.runtime.fileops.resource_limits import ARTIFACT_HARD_LIMIT_BYTES, ARTIFACT_WRITE_TTL_SECONDS, snapshot_budget
from mote.runtime.fileops.transactions import ScopedMutationArtifacts
from mote.runtime.session.events import MessageEvent, SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.ztest.fileops_factory import FileMutationArtifactRepository


def _components(root, session_id="session"):
    artifacts = FileMutationArtifactRepository(
        root / "artifacts",
        hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
    )
    reader = SealedSnapshotReader(artifacts)
    locks = HierarchicalLockManager(root / "locks")
    publisher = AtomicPublisher(artifacts)
    journal = DurableFileOperationsJournal(
        root / session_id / "rollout.jsonl",
        session_id=session_id,
        locks=locks,
    )
    control = ProjectOperationControl(locks)
    coordinator = MutationCoordinator(
        session_id=session_id,
        artifacts=artifacts,
        reader=reader,
        locks=locks,
        publisher=publisher,
        journal=journal,
        control=control,
    )
    control.register(coordinator)
    return coordinator, reader, publisher, journal


def _mutation_factory(coordinator, project_root):
    return MutationFactory(
        session_id=coordinator.session_id,
        artifacts=coordinator.artifacts,
        get_project_root=lambda: os.fspath(project_root),
    )


def _capture(coordinator, reader, target, project_root, *, encoding=None):
    scope = coordinator.artifacts.write_scope(
        owner="test-transaction-snapshot",
        maximum_bytes=snapshot_budget(os.stat(target).st_size),
        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
    )
    with scope:
        snapshot = reader.open_snapshot(
            target,
            scope=scope,
            project_root=project_root,
            encoding=encoding,
        )
        scope.complete(durability_root=project_root)
        return snapshot


def _mutation_scope(coordinator, maximum_bytes, *, owner="test-transaction"):
    return coordinator.artifacts.write_scope(
        owner=owner,
        maximum_bytes=maximum_bytes,
        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
    )


def _creation_budget(content):
    return len(content) + len(encode_metadata_manifest(PreservedMetadata.for_create()))


def _artifact_bytes(coordinator, digest):
    artifact = coordinator.artifacts.resolve_live(digest)
    return coordinator.artifacts.read_bytes(artifact)


def test_journal_ignores_foreign_events_but_fails_closed_on_corrupt_fileops(
    tmp_path,
):
    _, _, _, journal = _components(tmp_path)
    log = SessionLog("session", base_dir=str(tmp_path))
    log.commit_offline(
        SessionMetaEvent(
            session_id="session",
            role_class="mote.file_operations.v1",
            toolset_manifest=(),
        )
    )
    log.commit_offline(MessageEvent(message=UserMessage(content="ok")))
    assert journal.records() == ()
    with journal.path.open("ab") as stream:
        stream.write(b"{not-json}\n")

    with pytest.raises(JournalDurabilityError) as exc_info:
        journal.records()

    assert exc_info.value.context["path"] == str(journal.path)


def test_replace_records_prepared_and_committed_around_publish(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    coordinator, reader, _, journal = _components(tmp_path)
    snapshot = _capture(coordinator, reader, target, tmp_path)

    factory = _mutation_factory(coordinator, tmp_path)
    scope = _mutation_scope(coordinator, len(b"after"))
    with scope:
        mutation_set = factory.mutation_set(
            source="Edit",
            mutations=(factory.replacement(snapshot, b"after", scope=scope),),
        )
        result = coordinator.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
        )

    assert result.status == TransactionStatus.COMMITTED
    assert target.read_bytes() == b"after"
    events = tuple(journal.iter_events())
    assert [type(event) for event in events] == [
        FileTransactionPreparedEvent,
        FileTransactionCommittedEvent,
    ]
    record = journal.get(result.transaction_id)
    assert record is not None
    assert record.status == TransactionStatus.COMMITTED
    assert record.mutation_set.mutations[0].before == snapshot
    assert record.mutation_set.mutations[0].after.digest == result.versions[0].digest


def test_resume_returns_existing_settlement_without_rebuilding_mutation(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    coordinator, reader, _, _ = _components(tmp_path)
    snapshot = _capture(coordinator, reader, target, tmp_path)
    factory = _mutation_factory(coordinator, tmp_path)
    scope = _mutation_scope(coordinator, len(b"after"))
    with scope:
        mutation_set = factory.mutation_set(
            source="GenerateMedia:image:0",
            transaction_id="stable-publication",
            mutations=(factory.replacement(snapshot, b"after", scope=scope),),
        )
        committed = coordinator.commit(mutation_set, ScopedMutationArtifacts(scope))

    assert coordinator.resume("stable-publication") == committed
    assert coordinator.resume("missing") is None
    assert target.read_bytes() == b"after"


def test_create_and_delete_use_the_same_durable_protocol(tmp_path):
    target = tmp_path / "nested" / "target.txt"
    target.parent.mkdir()
    coordinator, reader, _, journal = _components(tmp_path)

    factory = _mutation_factory(coordinator, tmp_path)
    create_scope = _mutation_scope(coordinator, _creation_budget(b"created"))
    with create_scope:
        create_set = factory.mutation_set(
            source="Edit",
            mutations=(
                factory.creation(
                    os.fspath(target),
                    b"created",
                    scope=create_scope,
                ),
            ),
        )
        created = coordinator.commit(
            create_set,
            ScopedMutationArtifacts(create_scope),
        )
    snapshot = _capture(coordinator, reader, target, tmp_path)
    delete_set = factory.mutation_set(
        source="history_restore",
        mutations=(factory.deletion(snapshot),),
    )
    delete_scope = _mutation_scope(coordinator, 0)
    with delete_scope:
        deleted = coordinator.commit(
            delete_set,
            ScopedMutationArtifacts(delete_scope),
        )

    assert created.status == TransactionStatus.COMMITTED
    assert deleted.status == TransactionStatus.COMMITTED
    assert not target.exists()
    assert [record.status for record in journal.records()] == [
        TransactionStatus.COMMITTED,
        TransactionStatus.COMMITTED,
    ]
    assert list(target.parent.glob(".target.txt.mote-delete-*.tombstone")) == []


def test_replace_hunks_use_sealed_encoding_and_normalize_mixed_newlines(tmp_path):
    target = tmp_path / "legacy.txt"
    before = "头\r\n旧\n尾\r"
    after = "头\r\n新\n尾\r"
    target.write_bytes(before.encode("gbk"))
    coordinator, reader, _, journal = _components(tmp_path)
    snapshot = _capture(
        coordinator,
        reader,
        target,
        tmp_path,
        encoding="gbk",
    )

    factory = _mutation_factory(coordinator, tmp_path)
    normalized_before = before.replace("\r\n", "\n").replace("\r", "\n")
    normalized_after = after.replace("\r\n", "\n").replace("\r", "\n")
    scope = _mutation_scope(
        coordinator,
        len(after.encode("gbk")) + len(normalized_before.encode("utf-8")) + len(normalized_after.encode("utf-8")),
    )
    with scope:
        mutation_set = factory.mutation_set(
            source="Edit",
            mutations=(
                factory.replacement(
                    snapshot,
                    after.encode("gbk"),
                    scope=scope,
                ),
            ),
        )
        coordinator.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
            hunks=coordinator.build_hunks(
                mutation_set,
                turn_index=4,
                scope=scope,
            ),
        )

    (record,) = journal.review_records()
    assert _artifact_bytes(coordinator, record.pre_hash) == "头\n旧\n尾\n".encode()
    assert _artifact_bytes(coordinator, record.post_hash) == "头\n新\n尾\n".encode()


def test_replace_hunks_reject_after_bytes_without_the_sealed_bom(tmp_path):
    target = tmp_path / "utf16.txt"
    original = codecs.BOM_UTF16_LE + "before\r\n".encode("utf-16-le")
    target.write_bytes(original)
    coordinator, reader, _, journal = _components(tmp_path)
    snapshot = _capture(
        coordinator,
        reader,
        target,
        tmp_path,
        encoding="utf-16-le",
    )

    with pytest.raises(EncodingRejectedError, match="sealed BOM"):
        factory = _mutation_factory(coordinator, tmp_path)
        scope = _mutation_scope(coordinator, len("after\n".encode("utf-8")))
        with scope:
            mutation_set = factory.mutation_set(
                source="Edit",
                mutations=(
                    factory.replacement(
                        snapshot,
                        "after\n".encode("utf-8"),
                        scope=scope,
                    ),
                ),
            )
            coordinator.commit(
                mutation_set,
                ScopedMutationArtifacts(scope),
                hunks=coordinator.build_hunks(
                    mutation_set,
                    turn_index=5,
                    scope=scope,
                ),
            )

    assert target.read_bytes() == original
    assert journal.records() == ()


def test_replace_hunks_require_a_sealed_snapshot_encoding(tmp_path):
    target = tmp_path / "untyped.txt"
    target.write_bytes(b"before\n")
    coordinator, reader, _, journal = _components(tmp_path)
    snapshot = _capture(coordinator, reader, target, tmp_path)

    with pytest.raises(EncodingRejectedError, match="sealed snapshot encoding"):
        factory = _mutation_factory(coordinator, tmp_path)
        scope = _mutation_scope(coordinator, len(b"after\n"))
        with scope:
            mutation_set = factory.mutation_set(
                source="Edit",
                mutations=(factory.replacement(snapshot, b"after\n", scope=scope),),
            )
            coordinator.commit(
                mutation_set,
                ScopedMutationArtifacts(scope),
                hunks=coordinator.build_hunks(
                    mutation_set,
                    turn_index=6,
                    scope=scope,
                ),
            )

    assert target.read_bytes() == b"before\n"
    assert journal.records() == ()


def test_delete_recovery_commits_renamed_tombstone_and_cleans_it(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    coordinator, reader, publisher, journal = _components(tmp_path)
    snapshot = _capture(coordinator, reader, target, tmp_path)
    original_append = journal.append

    def fail_first_commit(event):
        if isinstance(event, FileTransactionCommittedEvent):
            monkeypatch.setattr(journal, "append", original_append)
            raise FilePublishError("injected journal failure")
        original_append(event)

    monkeypatch.setattr(journal, "append", fail_first_commit)
    factory = _mutation_factory(coordinator, tmp_path)
    mutation_set = factory.mutation_set(
        source="history_restore",
        mutations=(factory.deletion(snapshot),),
    )
    with pytest.raises(FilePublishError):
        scope = _mutation_scope(coordinator, 0)
        with scope:
            coordinator.commit(
                mutation_set,
                ScopedMutationArtifacts(scope),
            )

    assert not target.exists()
    tombstones = list(tmp_path.glob(".target.txt.mote-delete-*.tombstone"))
    assert tombstones == []
    assert journal.records()[0].status == TransactionStatus.COMMITTED


def test_create_conflict_fails_before_prepared(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"external")
    coordinator, _, _, journal = _components(tmp_path)
    factory = _mutation_factory(coordinator, tmp_path)

    with pytest.raises(StaleSnapshotError):
        scope = _mutation_scope(coordinator, _creation_budget(b"created"))
        with scope:
            mutation_set = factory.mutation_set(
                source="Edit",
                mutations=(
                    factory.creation(
                        os.fspath(target),
                        b"created",
                        scope=scope,
                    ),
                ),
            )
            coordinator.commit(
                mutation_set,
                ScopedMutationArtifacts(scope),
            )

    assert target.read_bytes() == b"external"
    assert journal.records() == ()


def test_stale_snapshot_fails_before_prepared(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    coordinator, reader, _, journal = _components(tmp_path)
    snapshot = _capture(coordinator, reader, target, tmp_path)
    target.write_bytes(b"external")
    factory = _mutation_factory(coordinator, tmp_path)
    with pytest.raises(StaleSnapshotError):
        scope = _mutation_scope(coordinator, len(b"after"))
        with scope:
            mutation_set = factory.mutation_set(
                source="Edit",
                mutations=(factory.replacement(snapshot, b"after", scope=scope),),
            )
            coordinator.commit(
                mutation_set,
                ScopedMutationArtifacts(scope),
            )

    assert target.read_bytes() == b"external"
    assert journal.records() == ()


def test_final_compare_rejects_external_change_after_prepared(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    coordinator, reader, publisher, journal = _components(tmp_path)
    snapshot = _capture(coordinator, reader, target, tmp_path)
    original_verify = publisher._verify_expected

    def external_write_before_verify(path, expected):
        target.write_bytes(b"external")
        return original_verify(path, expected)

    monkeypatch.setattr(publisher, "_verify_expected", external_write_before_verify)
    factory = _mutation_factory(coordinator, tmp_path)
    with pytest.raises((ContentChangedError, StaleSnapshotError)):
        scope = _mutation_scope(coordinator, len(b"after"))
        with scope:
            mutation_set = factory.mutation_set(
                source="Edit",
                mutations=(factory.replacement(snapshot, b"after", scope=scope),),
            )
            coordinator.commit(
                mutation_set,
                ScopedMutationArtifacts(scope),
            )

    assert target.read_bytes() == b"external"
    assert journal.records()[0].status == TransactionStatus.IN_DOUBT


def test_publish_failure_before_replace_is_durably_aborted(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    coordinator, reader, publisher, journal = _components(tmp_path)
    snapshot = _capture(coordinator, reader, target, tmp_path)

    def fail_publish(*args, **kwargs):
        raise FilePublishError("injected")

    monkeypatch.setattr(publisher, "replace_from_blob", fail_publish)
    factory = _mutation_factory(coordinator, tmp_path)
    with pytest.raises(FilePublishError):
        scope = _mutation_scope(coordinator, len(b"after"))
        with scope:
            mutation_set = factory.mutation_set(
                source="Edit",
                mutations=(factory.replacement(snapshot, b"after", scope=scope),),
            )
            coordinator.commit(
                mutation_set,
                ScopedMutationArtifacts(scope),
            )

    assert target.read_bytes() == b"before"
    assert journal.records()[0].status == TransactionStatus.ABORTED


class _CrashJournal(DurableFileOperationsJournal):
    def __init__(self, *args, crash_stage: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._crash_stage = crash_stage

    def append(self, event):
        if self._crash_stage == "before_prepared" and isinstance(event, FileTransactionPreparedEvent):
            os._exit(90)
        if self._crash_stage == "before_committed" and isinstance(event, FileTransactionCommittedEvent):
            os._exit(92)
        super().append(event)
        if self._crash_stage == "after_committed" and isinstance(event, FileTransactionCommittedEvent):
            os._exit(93)
        if self._crash_stage == "after_prepared" and isinstance(event, FileTransactionPreparedEvent):
            os._exit(91)


class _PausingJournal(DurableFileOperationsJournal):
    def __init__(self, *args, prepared, release, **kwargs):
        super().__init__(*args, **kwargs)
        self._prepared = prepared
        self._release = release

    def append(self, event):
        super().append(event)
        if isinstance(event, FileTransactionPreparedEvent):
            self._prepared.set()
            self._release.wait(60)


def _crash_transaction(root_text: str, crash_stage: str) -> None:
    root = Path(root_text)
    target = root / "target.txt"
    artifacts = FileMutationArtifactRepository(
        root / "artifacts",
        hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
    )
    reader = SealedSnapshotReader(artifacts)
    locks = HierarchicalLockManager(root / "locks")
    publisher = AtomicPublisher(artifacts)
    journal = _CrashJournal(
        root / "session" / "rollout.jsonl",
        session_id="session",
        locks=locks,
        crash_stage=crash_stage,
    )
    coordinator = MutationCoordinator(
        session_id="session",
        artifacts=artifacts,
        reader=reader,
        locks=locks,
        publisher=publisher,
        journal=journal,
        control=ProjectOperationControl(locks),
    )
    coordinator.control.register(coordinator)
    snapshot = _capture(
        coordinator,
        reader,
        target,
        root,
        encoding="utf-8",
    )
    factory = _mutation_factory(coordinator, root)
    scope = _mutation_scope(coordinator, len(b"afterbeforeafter"))
    with scope:
        mutation_set = factory.mutation_set(
            source="Edit",
            mutations=(factory.replacement(snapshot, b"after", scope=scope),),
        )
        coordinator.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
            hunks=coordinator.build_hunks(
                mutation_set,
                turn_index=1,
                scope=scope,
            ),
        )


def _crash_create_or_delete(root_text: str, operation: str) -> None:
    root = Path(root_text)
    target = root / "target.txt"
    artifacts = FileMutationArtifactRepository(
        root / "artifacts",
        hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
    )
    reader = SealedSnapshotReader(artifacts)
    locks = HierarchicalLockManager(root / "locks")
    publisher = AtomicPublisher(artifacts)
    journal = _CrashJournal(
        root / "session" / "rollout.jsonl",
        session_id="session",
        locks=locks,
        crash_stage="before_committed",
    )
    coordinator = MutationCoordinator(
        session_id="session",
        artifacts=artifacts,
        reader=reader,
        locks=locks,
        publisher=publisher,
        journal=journal,
        control=ProjectOperationControl(locks),
    )
    coordinator.control.register(coordinator)
    factory = _mutation_factory(coordinator, root)
    if operation == "create":
        scope = _mutation_scope(
            coordinator,
            _creation_budget(b"created"),
            owner="test-crash-create",
        )
    else:
        snapshot = _capture(coordinator, reader, target, root)
        scope = _mutation_scope(coordinator, 0, owner="test-crash-delete")
    with scope:
        if operation == "create":
            mutation = factory.creation(
                os.fspath(target),
                b"created",
                scope=scope,
            )
        else:
            mutation = factory.deletion(snapshot)
        mutation_set = factory.mutation_set(
            source="Edit" if operation == "create" else "history_restore",
            mutations=(mutation,),
        )
        coordinator.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
        )


def _race_replace(root_text: str, snapshot, replacement: bytes, start, outcomes) -> None:
    root = Path(root_text)
    coordinator, _, _, _ = _components(root, session_id="race")
    factory = _mutation_factory(coordinator, root)
    start.wait(5)
    try:
        scope = _mutation_scope(coordinator, len(replacement), owner="test-race")
        with scope:
            mutation_set = factory.mutation_set(
                source="Edit",
                mutations=(factory.replacement(snapshot, replacement, scope=scope),),
            )
            result = coordinator.commit(
                mutation_set,
                ScopedMutationArtifacts(scope),
            )
        outcomes.put(("committed", result.transaction_id))
    except Exception as exc:
        outcomes.put((type(exc).__name__, ""))


def _pause_distinct_replace(
    root_text: str,
    target_name: str,
    prepared,
    release,
    outcomes,
) -> None:
    root = Path(root_text)
    artifacts = FileMutationArtifactRepository(
        root / "artifacts",
        hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
    )
    reader = SealedSnapshotReader(artifacts)
    locks = HierarchicalLockManager(root / "locks")
    journal = _PausingJournal(
        root / "multi" / "rollout.jsonl",
        session_id="multi",
        locks=locks,
        prepared=prepared,
        release=release,
    )
    coordinator = MutationCoordinator(
        session_id="multi",
        artifacts=artifacts,
        reader=reader,
        locks=locks,
        publisher=AtomicPublisher(artifacts),
        journal=journal,
        control=ProjectOperationControl(locks),
    )
    coordinator.control.register(coordinator)
    target = root / target_name
    snapshot = _capture(coordinator, reader, target, root)
    factory = _mutation_factory(coordinator, root)
    replacement = target_name.encode()
    scope = _mutation_scope(coordinator, len(replacement), owner="test-distinct")
    with scope:
        mutation_set = factory.mutation_set(
            source="Edit",
            mutations=(factory.replacement(snapshot, replacement, scope=scope),),
        )
        result = coordinator.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
        )
    outcomes.put(result.status.value)


def _recover_project(root_text: str, entered, finished, outcomes) -> None:
    root = Path(root_text)
    coordinator, _, _, _ = _components(root, session_id="recovery")
    entered.set()
    try:
        coordinator.control.reconcile(
            project_identity(root),
            label=str(root),
        )
        outcomes.put("recovered")
    finally:
        finished.set()


@pytest.mark.parametrize(
    ("stage", "exitcode", "expected_status", "expected_content"),
    [
        ("after_prepared", 91, TransactionStatus.ABORTED, b"before"),
        ("before_committed", 92, TransactionStatus.COMMITTED, b"after"),
    ],
)
def test_process_crash_reconciles_from_durable_state(tmp_path, stage, exitcode, expected_status, expected_content):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_transaction,
        args=(str(tmp_path), stage),
    )
    process.start()
    process.join(10)
    assert process.exitcode == exitcode

    coordinator, _, _, journal = _components(tmp_path)
    coordinator.control.reconcile(
        project_identity(tmp_path),
        label=str(tmp_path),
    )

    assert target.read_bytes() == expected_content
    assert journal.records()[0].status == expected_status
    expected_hunk_count = 1 if expected_status == TransactionStatus.COMMITTED else 0
    assert len(journal.review_records()) == expected_hunk_count

    coordinator.control.reconcile(
        project_identity(tmp_path),
        label=str(tmp_path),
    )
    assert len(journal.review_records()) == expected_hunk_count


def test_foreign_session_reconciles_abandoned_mutation_fence(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_transaction,
        args=(str(tmp_path), "after_prepared"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 91

    foreign, _, _, _ = _components(tmp_path, session_id="foreign")
    foreign.control.reconcile(
        project_identity(tmp_path),
        label=str(tmp_path),
    )

    _, _, _, owner_journal = _components(tmp_path)
    assert owner_journal.records()[0].status == TransactionStatus.ABORTED
    assert target.read_bytes() == b"before"
    assert foreign.control.reservations(owner_journal.records()[0].mutation_set.mutations[0].project_identity) == ()


@pytest.mark.parametrize(
    ("stage", "exitcode", "expected_status", "expected_content"),
    [
        ("before_prepared", 90, None, b"before"),
        ("after_committed", 93, TransactionStatus.COMMITTED, b"after"),
    ],
)
def test_foreign_session_clears_terminal_or_unprepared_mutation_fence(
    tmp_path,
    stage,
    exitcode,
    expected_status,
    expected_content,
):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_transaction,
        args=(str(tmp_path), stage),
    )
    process.start()
    process.join(10)
    assert process.exitcode == exitcode

    foreign, _, _, _ = _components(tmp_path, session_id="foreign")
    foreign.control.reconcile(
        project_identity(tmp_path),
        label=str(tmp_path),
    )
    _, _, _, owner_journal = _components(tmp_path)

    records = owner_journal.records()
    assert (records[0].status if records else None) == expected_status
    assert target.read_bytes() == expected_content
    project = project_identity(tmp_path)
    assert foreign.control.reservations(project) == ()


@pytest.mark.parametrize(
    ("operation", "initial", "expected"),
    [
        ("create", None, b"created"),
        ("delete", b"before", None),
    ],
)
def test_create_delete_crash_after_publish_reconciles_committed(tmp_path, operation, initial, expected):
    target = tmp_path / "target.txt"
    if initial is not None:
        target.write_bytes(initial)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_create_or_delete,
        args=(str(tmp_path), operation),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 92

    coordinator, _, _, journal = _components(tmp_path)
    coordinator.control.reconcile(
        project_identity(tmp_path),
        label=str(tmp_path),
    )
    assert journal.records()[0].status == TransactionStatus.COMMITTED
    if expected is None:
        assert not target.exists()
        assert list(tmp_path.glob(".target.txt.mote-delete-*.tombstone")) == []
    else:
        assert target.read_bytes() == expected
    assert journal.records()[0].status == TransactionStatus.COMMITTED


def test_recovery_never_overwrites_unrelated_external_state(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_transaction,
        args=(str(tmp_path), "after_prepared"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 91
    target.write_bytes(b"external")

    coordinator, _, _, _ = _components(tmp_path, session_id="foreign")
    with pytest.raises(RecoveryInDoubtError):
        coordinator.control.reconcile(
            project_identity(tmp_path),
            label=str(tmp_path),
        )

    assert target.read_bytes() == b"external"
    _, _, _, owner_journal = _components(tmp_path)
    assert owner_journal.records()[0].status == TransactionStatus.IN_DOUBT


def test_two_processes_from_same_snapshot_cannot_both_commit(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    coordinator, reader, _, _ = _components(tmp_path, session_id="race")
    snapshot = _capture(coordinator, reader, target, tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_race_replace,
            args=(str(tmp_path), snapshot, content, start, outcomes),
        )
        for content in (b"first", b"second")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    results = [outcomes.get(timeout=2)[0] for _ in processes]
    assert results.count("committed") == 1
    assert results.count("StaleSnapshotError") == 1
    assert target.read_bytes() in {b"first", b"second"}
    _, _, _, journal = _components(tmp_path, session_id="race")
    records = journal.records()
    assert len(records) == 1
    assert records[0].status == TransactionStatus.COMMITTED


def test_distinct_file_transactions_publish_independent_project_fences(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    prepared = [context.Event(), context.Event()]
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_pause_distinct_replace,
            args=(str(tmp_path), name, ready, release, outcomes),
        )
        for name, ready in zip(("a.txt", "b.txt"), prepared)
    ]
    for process in processes:
        process.start()
    assert all(ready.wait(30) for ready in prepared)

    coordinator, _, _, _ = _components(tmp_path, session_id="multi")
    project = project_identity(tmp_path)
    assert len(coordinator.control.reservations(project)) == 2

    recovery_entered = context.Event()
    recovery_finished = context.Event()
    recovery_process = context.Process(
        target=_recover_project,
        args=(
            str(tmp_path),
            recovery_entered,
            recovery_finished,
            outcomes,
        ),
    )
    recovery_process.start()
    assert recovery_entered.wait(30)
    assert not recovery_finished.wait(0.3)

    release.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    recovery_process.join(10)
    assert recovery_process.exitcode == 0
    results = [outcomes.get(timeout=2) for _ in range(3)]
    assert results.count("committed") == 2
    assert results.count("recovered") == 1
    assert coordinator.control.reservations(project) == ()


def test_conflicting_mutation_recovers_abandoned_prepared_before_commit(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_transaction,
        args=(str(tmp_path), "after_prepared"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 91

    foreign, reader, _, foreign_journal = _components(
        tmp_path,
        session_id="foreign",
    )
    snapshot = _capture(foreign, reader, target, tmp_path)
    factory = _mutation_factory(foreign, tmp_path)
    scope = _mutation_scope(foreign, len(b"foreign"), owner="test-foreign")
    with scope:
        mutation_set = factory.mutation_set(
            source="Edit",
            mutations=(factory.replacement(snapshot, b"foreign", scope=scope),),
        )
        result = foreign.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
        )

    assert result.status == TransactionStatus.COMMITTED
    assert target.read_bytes() == b"foreign"
    _, _, _, owner_journal = _components(tmp_path)
    assert owner_journal.records()[0].status == TransactionStatus.ABORTED
    assert foreign_journal.records()[0].status == TransactionStatus.COMMITTED


def test_in_doubt_mutation_only_blocks_intersecting_scope(tmp_path):
    target = tmp_path / "target.txt"
    unrelated = tmp_path / "unrelated.txt"
    target.write_bytes(b"before")
    unrelated.write_bytes(b"unrelated")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_transaction,
        args=(str(tmp_path), "after_prepared"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 91
    target.write_bytes(b"external")

    foreign, reader, _, _ = _components(tmp_path, session_id="foreign")
    with pytest.raises(RecoveryInDoubtError):
        foreign.control.reconcile(
            project_identity(tmp_path),
            label=str(tmp_path),
        )

    unrelated_snapshot = _capture(foreign, reader, unrelated, tmp_path)
    factory = _mutation_factory(foreign, tmp_path)
    unrelated_scope = _mutation_scope(
        foreign,
        len(b"updated"),
        owner="test-unrelated",
    )
    with unrelated_scope:
        unrelated_set = factory.mutation_set(
            source="Edit",
            mutations=(
                factory.replacement(
                    unrelated_snapshot,
                    b"updated",
                    scope=unrelated_scope,
                ),
            ),
        )
        result = foreign.commit(
            unrelated_set,
            ScopedMutationArtifacts(unrelated_scope),
        )
    assert result.status == TransactionStatus.COMMITTED
    assert unrelated.read_bytes() == b"updated"

    target_snapshot = _capture(foreign, reader, target, tmp_path)
    with pytest.raises(RecoveryInDoubtError):
        target_scope = _mutation_scope(
            foreign,
            len(b"forbidden"),
            owner="test-blocked",
        )
        with target_scope:
            target_set = factory.mutation_set(
                source="Edit",
                mutations=(
                    factory.replacement(
                        target_snapshot,
                        b"forbidden",
                        scope=target_scope,
                    ),
                ),
            )
            foreign.commit(
                target_set,
                ScopedMutationArtifacts(target_scope),
            )
    assert target.read_bytes() == b"external"


def test_unprepared_reservation_only_blocks_its_own_scope(tmp_path):
    target = tmp_path / "target.txt"
    unrelated = tmp_path / "unrelated.txt"
    target.write_bytes(b"before")
    unrelated.write_bytes(b"unrelated")
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_transaction,
        args=(str(tmp_path), "before_prepared"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 90

    foreign, reader, _, _ = _components(tmp_path, session_id="foreign")
    unrelated_snapshot = _capture(foreign, reader, unrelated, tmp_path)
    factory = _mutation_factory(foreign, tmp_path)
    unrelated_scope = _mutation_scope(
        foreign,
        len(b"updated"),
        owner="test-unrelated",
    )
    with unrelated_scope:
        unrelated_set = factory.mutation_set(
            source="Edit",
            mutations=(
                factory.replacement(
                    unrelated_snapshot,
                    b"updated",
                    scope=unrelated_scope,
                ),
            ),
        )
        result = foreign.commit(
            unrelated_set,
            ScopedMutationArtifacts(unrelated_scope),
        )

    assert result.status == TransactionStatus.COMMITTED
    assert unrelated.read_bytes() == b"updated"
    assert len(foreign.control.reservations(project_identity(tmp_path))) == 1

    target_snapshot = _capture(foreign, reader, target, tmp_path)
    target_scope = _mutation_scope(
        foreign,
        len(b"recovered"),
        owner="test-recovered",
    )
    with target_scope:
        target_set = factory.mutation_set(
            source="Edit",
            mutations=(
                factory.replacement(
                    target_snapshot,
                    b"recovered",
                    scope=target_scope,
                ),
            ),
        )
        recovered = foreign.commit(
            target_set,
            ScopedMutationArtifacts(target_scope),
        )
    assert recovered.status == TransactionStatus.COMMITTED
    assert foreign.control.reservations(project_identity(tmp_path)) == ()
