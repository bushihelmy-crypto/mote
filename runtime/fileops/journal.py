"""Crash-durable File Operations events stored in the session rollout."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Optional

from mote.contracts.fileops.errors import JournalDurabilityError, ReviewConflictError
from mote.contracts.fileops.event_codec import event_from_line, event_to_line
from mote.contracts.fileops.events import (
    FileEditPlanStoredEvent,
    FileOperationsEvent,
    FileTransactionAbortedEvent,
    FileTransactionCommittedEvent,
    FileTransactionInDoubtEvent,
    FileTransactionPreparedEvent,
    HunkDetectedEvent,
    HunkReviewTransitionedEvent,
    RewindAbortedEvent,
    RewindCommittedEvent,
    RewindInDoubtEvent,
    RewindPreparedEvent,
)
from mote.contracts.fileops.models import (
    BlobRef,
    HunkRecord,
    LockMode,
    LockSpec,
    ReviewStatus,
    RewindRecord,
    TransactionRecord,
    TransactionStatus,
)
from mote.runtime.disk import disk_io
from mote.runtime.fileops.locking import JOURNAL_LOCK_LEVEL, HierarchicalLockManager


class DurableFileOperationsJournal:
    """Synchronous fsync barrier and projections for File Operations events."""

    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        locks: HierarchicalLockManager,
        flush_pending: Optional[Callable[[], None]] = None,
        event_sink: Callable[[FileOperationsEvent], object] | None = None,
        event_source: Callable[[], Iterable[FileOperationsEvent]] | None = None,
    ) -> None:
        if (event_sink is None) != (event_source is None):
            raise ValueError("event_sink and event_source must be provided together")
        self.path = Path(path)
        self.session_id = session_id
        self._locks = locks
        self._flush_pending = flush_pending
        self._event_sink = event_sink
        self._event_source = event_source
        self._key = hashlib.sha256(os.fsencode(self.path.absolute())).hexdigest()

    def append(self, event: FileOperationsEvent) -> None:
        try:
            if self._flush_pending is not None:
                self._flush_pending()
            with self._locks.acquire_many([self._journal_lock_spec(LockMode.EXCLUSIVE)]):
                self._append_unlocked(event)
        except Exception as exc:
            if isinstance(exc, JournalDurabilityError):
                raise
            raise JournalDurabilityError(
                f"cannot durably append file transaction event to {self.path}",
                path=str(self.path),
                event_type=event.type,
                cause=exc,
            ) from exc

    def publish_edit_plan(self, plan_id: str, manifest: BlobRef) -> BlobRef:
        event = FileEditPlanStoredEvent(plan_id, manifest)
        try:
            self._flush()
            with self._locks.acquire_many([self._journal_lock_spec(LockMode.EXCLUSIVE)]):
                prior = self._edit_plan_manifest_unlocked(plan_id)
                if prior is not None:
                    if prior != manifest:
                        raise JournalDurabilityError(
                            "edit plan id resolves to conflicting manifests",
                            plan_id=plan_id,
                        )
                    return prior
                self._append_unlocked(event)
                return manifest
        except Exception as exc:
            if isinstance(exc, JournalDurabilityError):
                raise
            raise JournalDurabilityError(
                f"cannot durably publish edit plan to {self.path}",
                path=str(self.path),
                plan_id=plan_id,
                cause=exc,
            ) from exc

    def records(self) -> tuple[TransactionRecord, ...]:
        folded: dict[str, TransactionRecord] = {}
        for event in self.iter_events():
            if isinstance(event, FileTransactionPreparedEvent):
                folded[event.mutation_set.transaction_id] = TransactionRecord(
                    mutation_set=event.mutation_set,
                    status=TransactionStatus.PREPARED,
                    hunks=event.hunks,
                )
                continue
            if not isinstance(
                event,
                (
                    FileTransactionCommittedEvent,
                    FileTransactionAbortedEvent,
                    FileTransactionInDoubtEvent,
                ),
            ):
                continue
            prior = folded.get(event.transaction_id)
            if prior is None:
                continue
            if isinstance(event, FileTransactionCommittedEvent):
                folded[event.transaction_id] = TransactionRecord(
                    mutation_set=prior.mutation_set,
                    status=TransactionStatus.COMMITTED,
                    hunks=prior.hunks,
                    committed_versions=event.versions,
                )
            elif isinstance(event, FileTransactionAbortedEvent):
                folded[event.transaction_id] = TransactionRecord(
                    mutation_set=prior.mutation_set,
                    status=TransactionStatus.ABORTED,
                    hunks=prior.hunks,
                    detail=event.detail,
                )
            elif isinstance(event, FileTransactionInDoubtEvent):
                folded[event.transaction_id] = TransactionRecord(
                    mutation_set=prior.mutation_set,
                    status=TransactionStatus.IN_DOUBT,
                    hunks=prior.hunks,
                    detail=event.detail,
                )
        return tuple(folded.values())

    def get(self, transaction_id: str) -> Optional[TransactionRecord]:
        return next(
            (record for record in self.records() if record.mutation_set.transaction_id == transaction_id),
            None,
        )

    def pending(self) -> tuple[TransactionRecord, ...]:
        return tuple(record for record in self.records() if record.status == TransactionStatus.PREPARED)

    def edit_plan_manifest(self, plan_id: str) -> BlobRef | None:
        return self._fold_edit_plan_manifest(plan_id, self.iter_events())

    def _edit_plan_manifest_unlocked(self, plan_id: str) -> BlobRef | None:
        return self._fold_edit_plan_manifest(
            plan_id,
            self._iter_events_unlocked(),
        )

    @staticmethod
    def _fold_edit_plan_manifest(
        plan_id: str,
        events: Iterable[FileOperationsEvent],
    ) -> BlobRef | None:
        manifest: BlobRef | None = None
        for event in events:
            if not isinstance(event, FileEditPlanStoredEvent):
                continue
            if event.plan_id != plan_id:
                continue
            if manifest is not None and manifest != event.manifest:
                raise JournalDurabilityError(
                    "edit plan id resolves to conflicting manifests",
                    plan_id=plan_id,
                )
            manifest = event.manifest
        return manifest

    def review_records(self) -> tuple[HunkRecord, ...]:
        return tuple(self._fold_reviews(self.iter_events()).values())

    def rewind_records(self) -> tuple[RewindRecord, ...]:
        return tuple(self._fold_rewinds(self.iter_events()).values())

    def timeline_epoch(self) -> int:
        epoch = 0
        for event in self.iter_events():
            if not isinstance(event, RewindCommittedEvent):
                continue
            if event.source_epoch != epoch or event.target_epoch != epoch + 1:
                raise JournalDurabilityError(
                    "rewind timeline is not a contiguous monotonic sequence",
                    expected_source_epoch=epoch,
                    actual_source_epoch=event.source_epoch,
                    target_epoch=event.target_epoch,
                )
            epoch = event.target_epoch
        return epoch

    def rewind(self, transaction_id: str) -> Optional[RewindRecord]:
        return self._fold_rewinds(self.iter_events()).get(transaction_id)

    def pending_rewinds(self) -> tuple[RewindRecord, ...]:
        return tuple(record for record in self.rewind_records() if record.status == TransactionStatus.PREPARED)

    def review(self, hunk_id: str) -> Optional[HunkRecord]:
        return self._fold_reviews(self.iter_events()).get(hunk_id)

    def detect_hunk(self, record: HunkRecord) -> HunkRecord:
        try:
            self._flush()
            with self._locks.acquire_many([self._journal_lock_spec(LockMode.EXCLUSIVE)]):
                folded = self._fold_reviews(self._iter_events_unlocked())
                prior = folded.get(record.hunk_id)
                if prior is not None:
                    if prior == record:
                        return prior
                    raise ReviewConflictError(
                        f"hunk id already exists: {record.hunk_id}",
                        hunk_id=record.hunk_id,
                        version=prior.version,
                    )
                self._append_unlocked(HunkDetectedEvent(record))
                return record
        except ReviewConflictError:
            raise
        except Exception as exc:
            raise JournalDurabilityError(
                f"cannot durably append hunk detection to {self.path}",
                path=str(self.path),
                hunk_id=record.hunk_id,
                cause=exc,
            ) from exc

    def transition_hunk(
        self,
        hunk_id: str,
        *,
        expected_version: int,
        status: ReviewStatus,
        new_range: tuple[int, int],
        post_hash: str,
        expected_digest: str,
        child_transaction_id: str = "",
    ) -> HunkRecord:
        try:
            self._flush()
            with self._locks.acquire_many([self._journal_lock_spec(LockMode.EXCLUSIVE)]):
                folded = self._fold_reviews(self._iter_events_unlocked())
                prior = folded.get(hunk_id)
                if prior is None or prior.version != expected_version:
                    raise ReviewConflictError(
                        f"hunk review version changed: {hunk_id}",
                        hunk_id=hunk_id,
                        expected_version=expected_version,
                        actual_version=prior.version if prior is not None else None,
                    )
                updated = replace(
                    prior,
                    status=status,
                    new_range=new_range,
                    post_hash=post_hash,
                    expected_digest=expected_digest,
                    child_transaction_id=child_transaction_id,
                    version=expected_version + 1,
                )
                self._append_unlocked(
                    HunkReviewTransitionedEvent(
                        hunk_id=hunk_id,
                        expected_version=expected_version,
                        version=updated.version,
                        status=status,
                        new_range=new_range,
                        post_hash=post_hash,
                        expected_digest=expected_digest,
                        child_transaction_id=child_transaction_id,
                    )
                )
                return updated
        except ReviewConflictError:
            raise
        except Exception as exc:
            raise JournalDurabilityError(
                f"cannot durably append hunk review transition to {self.path}",
                path=str(self.path),
                hunk_id=hunk_id,
                expected_version=expected_version,
                cause=exc,
            ) from exc

    def iter_events(self) -> Iterable[FileOperationsEvent]:
        if self._event_source is None and not self.path.exists():
            return
        try:
            with self._locks.acquire_many([self._journal_lock_spec(LockMode.SHARED)]):
                yield from self._iter_events_unlocked()
        except Exception as exc:
            if isinstance(exc, JournalDurabilityError):
                raise
            raise JournalDurabilityError(
                f"cannot read file transaction journal {self.path}",
                path=str(self.path),
                cause=exc,
            ) from exc

    def _iter_events_unlocked(self) -> Iterable[FileOperationsEvent]:
        if self._event_source is not None:
            yield from self._event_source()
            return
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, raw in enumerate(stream, start=1):
                try:
                    event = event_from_line(raw)
                except ValueError as exc:
                    raise JournalDurabilityError(
                        "authoritative file operations journal is corrupt",
                        path=str(self.path),
                        line_number=line_number,
                        cause=exc,
                    ) from exc
                if event is not None:
                    yield event

    def _append_unlocked(self, event: FileOperationsEvent) -> None:
        if self._event_sink is not None:
            self._event_sink(event)
            return
        existed = self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        disk_io.append_line(self.path, event_to_line(event), fsync=True)
        if not existed:
            self._fsync_parent()

    def _flush(self) -> None:
        if self._flush_pending is not None:
            self._flush_pending()

    @staticmethod
    def _fold_reviews(events: Iterable[FileOperationsEvent]) -> dict[str, HunkRecord]:
        folded: dict[str, HunkRecord] = {}
        prepared_hunks: dict[str, tuple[HunkRecord, ...]] = {}
        for event in events:
            if isinstance(event, FileTransactionPreparedEvent):
                prepared_hunks[event.mutation_set.transaction_id] = event.hunks
                continue
            if isinstance(event, (FileTransactionAbortedEvent, FileTransactionInDoubtEvent)):
                prepared_hunks.pop(event.transaction_id, None)
                continue
            if isinstance(event, FileTransactionCommittedEvent):
                for record in prepared_hunks.pop(event.transaction_id, ()):
                    folded.setdefault(record.hunk_id, record)
                continue
            if isinstance(event, HunkDetectedEvent):
                folded.setdefault(event.record.hunk_id, event.record)
                continue
            if not isinstance(event, HunkReviewTransitionedEvent):
                continue
            prior = folded.get(event.hunk_id)
            if prior is None or prior.version != event.expected_version or event.version != event.expected_version + 1:
                continue
            folded[event.hunk_id] = replace(
                prior,
                status=event.status,
                new_range=event.new_range,
                post_hash=event.post_hash,
                expected_digest=event.expected_digest,
                child_transaction_id=event.child_transaction_id,
                version=event.version,
            )
        return folded

    @staticmethod
    def _fold_rewinds(events: Iterable[FileOperationsEvent]) -> dict[str, RewindRecord]:
        folded: dict[str, RewindRecord] = {}
        for event in events:
            if isinstance(event, RewindPreparedEvent):
                folded[event.transaction_id] = RewindRecord(
                    transaction_id=event.transaction_id,
                    session_id=event.session_id,
                    status=TransactionStatus.PREPARED,
                    project_identity=event.project_identity,
                    working_dir=event.working_dir,
                    safety_commit=event.safety_commit,
                    target_commit=event.target_commit,
                    prompt_index=event.prompt_index,
                    source_epoch=event.source_epoch,
                    external_paths=event.external_paths,
                )
                continue
            if not isinstance(
                event,
                (RewindCommittedEvent, RewindAbortedEvent, RewindInDoubtEvent),
            ):
                continue
            prior = folded.get(event.transaction_id)
            if prior is None or prior.status != TransactionStatus.PREPARED:
                continue
            if isinstance(event, RewindCommittedEvent):
                status = TransactionStatus.COMMITTED
                detail = ""
            elif isinstance(event, RewindAbortedEvent):
                status = TransactionStatus.ABORTED
                detail = event.detail
            else:
                status = TransactionStatus.IN_DOUBT
                detail = event.detail
            folded[event.transaction_id] = replace(
                prior,
                status=status,
                detail=detail,
            )
        return folded

    def _journal_lock_spec(self, mode: LockMode) -> LockSpec:
        return LockSpec(
            level=JOURNAL_LOCK_LEVEL,
            key=self._key,
            mode=mode,
            label=f"session journal {self.session_id}",
        )

    def _fsync_parent(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = ["DurableFileOperationsJournal"]
