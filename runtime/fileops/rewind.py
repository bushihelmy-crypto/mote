"""Project-exclusive, crash-reconcilable whole-worktree rewind."""

from __future__ import annotations

import uuid
from pathlib import Path

from mote.contracts.events.file.facts import (
    RewindAbortedEvent,
    RewindCommittedEvent,
    RewindInDoubtEvent,
    RewindPreparedEvent,
)
from mote.contracts.file.errors import RewindFailedError
from mote.contracts.file.recovery import RewindRecord, RewindResult
from mote.contracts.file.transactions import FileOperationKind, TransactionStatus
from mote.runtime.fileops.checkpoints import WorktreeCheckpointStore
from mote.runtime.fileops.control import ProjectOperationControl
from mote.runtime.fileops.cursor_registry import DurableCursorRegistry
from mote.runtime.fileops.fences import RecoveryFence
from mote.runtime.fileops.identity import project_identity
from mote.runtime.fileops.journal import DurableFileOperationsJournal
from mote.runtime.fileops.locking import HierarchicalLockManager


class RewindCoordinator:
    """Publishes rewind under the same project barrier as ordinary mutations."""

    operation_kind = FileOperationKind.REWIND

    def __init__(
        self,
        *,
        session_id: str,
        git_dir: Path,
        locks: HierarchicalLockManager,
        journal: DurableFileOperationsJournal,
        control: ProjectOperationControl,
        timeline: DurableCursorRegistry,
    ) -> None:
        self.session_id = session_id
        self.git_dir = Path(git_dir)
        self.locks = locks
        self.journal = journal
        self.control = control
        self.timeline = timeline

    def rewind(
        self,
        *,
        working_dir: str,
        target_commit: str,
        parent_commit: str | None,
        prompt_index: int,
        after_commit: str = "",
    ) -> RewindResult:
        project = project_identity(working_dir)
        transaction_id = uuid.uuid4().hex
        with self.control.project_operation(
            project=project,
            label=working_dir,
            transaction_id=transaction_id,
            session_id=self.session_id,
            journal_path=self.journal.path,
            operation=self.operation_kind,
        ):
            store = WorktreeCheckpointStore(self.git_dir, Path(working_dir))
            source_epoch = self.timeline.synchronize(self.journal.timeline_epoch()).epoch
            store.tree_id(target_commit)
            safety_commit = store.capture(
                parent=parent_commit,
                message="before rewind",
            )
            external_paths = tuple(store.diff_tree(after_commit, safety_commit) if after_commit else ())
            self.journal.append(
                RewindPreparedEvent(
                    transaction_id=transaction_id,
                    session_id=self.session_id,
                    project_identity=project,
                    working_dir=working_dir,
                    safety_commit=safety_commit,
                    target_commit=target_commit,
                    prompt_index=prompt_index,
                    source_epoch=source_epoch,
                    external_paths=external_paths,
                )
            )
            try:
                store.restore(target_commit)
                verification = store.capture(
                    parent=target_commit,
                    message=f"verify rewind {transaction_id}",
                )
                if not store.same_tree(verification, target_commit):
                    raise RewindFailedError(
                        "rewind verification did not match target checkpoint",
                        transaction_id=transaction_id,
                    )
                self.journal.append(
                    RewindCommittedEvent(
                        transaction_id,
                        source_epoch=source_epoch,
                        target_epoch=source_epoch + 1,
                    )
                )
                self.timeline.synchronize(source_epoch + 1)
            except Exception as exc:
                self._compensate_or_record_in_doubt(
                    transaction_id,
                    store,
                    safety_commit,
                    target_commit,
                )
                raise RewindFailedError(
                    "rewind failed",
                    transaction_id=transaction_id,
                    cause=exc,
                ) from exc
            return RewindResult(
                transaction_id=transaction_id,
                status=TransactionStatus.COMMITTED,
                safety_commit=safety_commit,
                target_commit=target_commit,
                external_paths=external_paths,
            )

    def capture_checkpoint(
        self,
        *,
        working_dir: str,
        parent_commit: str | None,
        message: str,
    ) -> str:
        project = project_identity(working_dir)
        with self.control.project_lease(project=project, label=working_dir):
            return WorktreeCheckpointStore(
                self.git_dir,
                Path(working_dir),
            ).capture(parent=parent_commit, message=message)

    def _reconcile(self, record: RewindRecord) -> RewindResult:
        self.timeline.synchronize(self.journal.timeline_epoch())
        latest = self.journal.rewind(record.transaction_id)
        if latest is None:
            raise RewindFailedError(
                "rewind disappeared during recovery",
                transaction_id=record.transaction_id,
            )
        if latest.status != TransactionStatus.PREPARED:
            return self._result(latest)
        store = WorktreeCheckpointStore(self.git_dir, Path(record.working_dir))
        probe = store.capture(message=f"recover rewind {record.transaction_id}")
        if store.same_tree(probe, record.target_commit):
            self.journal.append(
                RewindCommittedEvent(
                    record.transaction_id,
                    source_epoch=record.source_epoch,
                    target_epoch=record.source_epoch + 1,
                )
            )
            self.timeline.synchronize(record.source_epoch + 1)
            status = TransactionStatus.COMMITTED
            detail = ""
        elif store.same_tree(probe, record.safety_commit):
            detail = "prepared rewind had not changed the worktree"
            self.journal.append(RewindAbortedEvent(record.transaction_id, detail))
            status = TransactionStatus.ABORTED
        else:
            detail = "live worktree matches neither rewind safety nor target tree"
            self.journal.append(RewindInDoubtEvent(record.transaction_id, detail))
            status = TransactionStatus.IN_DOUBT
        return RewindResult(
            transaction_id=record.transaction_id,
            status=status,
            safety_commit=record.safety_commit,
            target_commit=record.target_commit,
            external_paths=record.external_paths,
            detail=detail,
        )

    def _compensate_or_record_in_doubt(
        self,
        transaction_id: str,
        store: WorktreeCheckpointStore,
        safety_commit: str,
        target_commit: str,
    ) -> None:
        try:
            store.restore(safety_commit)
            verification = store.capture(
                parent=safety_commit,
                message=f"compensate rewind {transaction_id}",
            )
            if not store.same_tree(verification, safety_commit):
                raise RewindFailedError("rewind compensation verification failed")
            self.journal.append(RewindAbortedEvent(transaction_id, "rewind failed and safety tree was restored"))
        except Exception:
            self.journal.append(
                RewindInDoubtEvent(
                    transaction_id,
                    "rewind failed and the live worktree could not be proven safe",
                )
            )

    def _journal_for_fence(self, fence: RecoveryFence) -> DurableFileOperationsJournal:
        if Path(fence.journal_path) == self.journal.path.absolute():
            return self.journal
        return DurableFileOperationsJournal(
            Path(fence.journal_path),
            session_id=fence.session_id,
            locks=self.locks,
        )

    def load_recovery_record(self, fence: RecoveryFence) -> RewindRecord | None:
        return self._journal_for_fence(fence).rewind(fence.transaction_id)

    @staticmethod
    def recovery_status(record: object) -> TransactionStatus:
        if not isinstance(record, RewindRecord):
            raise TypeError("rewind recovery received a non-rewind record")
        return record.status

    @staticmethod
    def recovery_paths(record: object) -> tuple[str, ...]:
        if not isinstance(record, RewindRecord):
            raise TypeError("rewind recovery received a non-rewind record")
        return (record.working_dir,)

    def reconcile_recovery_record(
        self,
        fence: RecoveryFence,
        record: object,
    ) -> TransactionStatus:
        if not isinstance(record, RewindRecord):
            raise TypeError("rewind recovery received a non-rewind record")
        journal = self._journal_for_fence(fence)
        coordinator = (
            self
            if journal is self.journal
            else RewindCoordinator(
                session_id=fence.session_id,
                git_dir=journal.path.parent / "git",
                locks=self.locks,
                journal=journal,
                control=self.control,
                timeline=DurableCursorRegistry(journal.path.parent / "cursor-registry.sqlite3"),
            )
        )
        return coordinator._reconcile(record).status

    def finalize_recovery_record(
        self,
        fence: RecoveryFence,
        record: object,
    ) -> None:
        if not isinstance(record, RewindRecord):
            raise TypeError("rewind recovery received a non-rewind record")

    @staticmethod
    def _result(record: RewindRecord) -> RewindResult:
        return RewindResult(
            transaction_id=record.transaction_id,
            status=record.status,
            safety_commit=record.safety_commit,
            target_commit=record.target_commit,
            external_paths=record.external_paths,
            detail=record.detail,
        )


__all__ = ["RewindCoordinator"]
