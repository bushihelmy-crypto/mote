"""Crash-durable File Operations events stored in the session rollout."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.events.envelope import EventEnvelope, JsonValue, StreamId
from mote.contracts.events.file.facts import (
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
from mote.contracts.file.errors import JournalDurabilityError, ReviewConflictError
from mote.contracts.file.identity import LockMode, LockSpec
from mote.contracts.file.recovery import RewindRecord
from mote.contracts.file.transactions import HunkRecord, ReviewStatus, TransactionRecord, TransactionStatus
from mote.runtime.events.journal import LocalEventJournal
from mote.runtime.fileops.locking import JOURNAL_LOCK_LEVEL, HierarchicalLockManager
from mote.runtime.session.codec import encode_session_event, iter_file_operations_events, session_stream_id
from mote.runtime.session.events import SessionMetaEvent


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
        self._stream_id = StreamId(session_stream_id(session_id))
        self._event_journal = None if event_sink is not None else LocalEventJournal(self.path, self._stream_id)
        self._schema_checked = False
        self._version = 0
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

    def publish_edit_plan(self, plan_id: str, manifest: ContentIdentity) -> ContentIdentity:
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

    def edit_plan_manifest(self, plan_id: str) -> ContentIdentity | None:
        return self._fold_edit_plan_manifest(plan_id, self.iter_events())

    def _edit_plan_manifest_unlocked(self, plan_id: str) -> ContentIdentity | None:
        return self._fold_edit_plan_manifest(
            plan_id,
            self._iter_events_unlocked(),
        )

    @staticmethod
    def _fold_edit_plan_manifest(
        plan_id: str,
        events: Iterable[FileOperationsEvent],
    ) -> ContentIdentity | None:
        manifest: ContentIdentity | None = None
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
        if self._event_journal is None:
            return
        self._ensure_current_schema()
        envelopes: Iterable[EventEnvelope[Mapping[str, JsonValue]]] = self._event_journal.iter_committed(
            self._stream_id
        )
        yield from iter_file_operations_events(envelopes)

    def _append_unlocked(self, event: FileOperationsEvent) -> None:
        if self._event_sink is not None:
            self._event_sink(event)
            return
        if self._event_journal is None:
            raise RuntimeError("file operations journal has no event sink")
        self._refresh_current_schema()
        if self._version == 0:
            self._append_session_event_unlocked(
                SessionMetaEvent(
                    session_id=self.session_id,
                    role_class="mote.file_operations.v1",
                    toolset_manifest=(),
                )
            )
        self._append_session_event_unlocked(event)

    def _append_session_event_unlocked(
        self,
        event: SessionMetaEvent | FileOperationsEvent,
    ) -> None:
        if self._event_journal is None:
            raise RuntimeError("file operations journal has no event journal")
        fact = encode_session_event(event, session_id=self.session_id)
        result = self._event_journal.append_committed(
            self._stream_id,
            (fact,),
            expected_version=self._version,
        )
        self._version = result.current_version

    def _ensure_current_schema(self) -> None:
        if self._schema_checked:
            return
        self._refresh_current_schema()

    def _refresh_current_schema(self) -> None:
        if self._event_journal is None:
            return
        self._event_journal.writer.flush_inline()
        report = self._event_journal.verify_committed(self._stream_id)
        if not report.valid:
            issue = report.issues[0]
            raise JournalDurabilityError(
                f"file operations journal integrity failure at line {issue.line}: {issue.detail}",
                path=str(self.path),
            )
        self._version = report.current_version
        self._schema_checked = True

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


__all__ = ["DurableFileOperationsJournal"]
