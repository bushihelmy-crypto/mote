"""Durable hunk review projection over the session File Operations journal."""

from __future__ import annotations

from mote.contracts.fileops.models import HunkRecord, ReviewStatus
from mote.contracts.text.hunks import split_hunks
from mote.runtime.fileops.artifact_repository import ArtifactWriteScope, ArtifactWriteScopeState
from mote.runtime.fileops.journal import DurableFileOperationsJournal

AGENT = "agent"
EXTERNAL = "external"
PENDING = ReviewStatus.PENDING
ACCEPTED = ReviewStatus.ACCEPTED
REJECTING = ReviewStatus.REJECTING
REJECTED = ReviewStatus.REJECTED


class ReviewService:
    """Records hunk facts and performs journal-locked expected-version transitions."""

    def __init__(
        self,
        *,
        session_id: str,
        journal: DurableFileOperationsJournal,
    ) -> None:
        self.session_id = session_id
        self.journal = journal

    def records(self) -> list[HunkRecord]:
        return list(self.journal.review_records())

    def status(self, hunk_id: str):
        return self.journal.review(hunk_id)

    def transaction(self, transaction_id: str):
        return self.journal.get(transaction_id)

    def pending(self) -> list[HunkRecord]:
        return [record for record in self.records() if record.status == PENDING]

    def for_turn(self, turn_index: int) -> list[HunkRecord]:
        return [record for record in self.records() if record.turn_index == turn_index]

    def for_path(self, path: str) -> list[HunkRecord]:
        return [record for record in self.records() if record.path == path]

    def record(self, record: HunkRecord) -> HunkRecord:
        return self.journal.detect_hunk(record)

    def record_delta(
        self,
        *,
        path: str,
        old: str,
        new: str,
        source: str,
        turn_index: int,
        tool_call_id: str = "",
        id_base: str,
        expected_digest: str,
        scope: ArtifactWriteScope,
    ) -> list[HunkRecord]:
        hunks = split_hunks(old, new)
        if not hunks:
            scope.complete(durability_root=self.journal.path.parent)
            return []
        pre_hash = scope.put_bytes(old.encode("utf-8")).digest
        post_hash = scope.put_bytes(new.encode("utf-8")).digest
        records = []
        for index, hunk in enumerate(hunks):
            record = HunkRecord(
                hunk_id=f"{id_base}:{index}",
                path=path,
                session_id=self.session_id,
                tool_call_id=tool_call_id,
                turn_index=turn_index,
                source=source,
                old_range=(hunk.old_start, hunk.old_count),
                new_range=(hunk.new_start, hunk.new_count),
                pre_hash=pre_hash,
                post_hash=post_hash,
                expected_digest=expected_digest,
            )
            records.append(self.record(record))
            if scope.state == ArtifactWriteScopeState.ACTIVE:
                scope.complete(durability_root=self.journal.path.parent)
        return records

    def transition(
        self,
        record: HunkRecord,
        *,
        status: ReviewStatus,
        new_range: tuple[int, int] | None = None,
        post_hash: str | None = None,
        expected_digest: str | None = None,
        child_transaction_id: str = "",
    ) -> HunkRecord:
        return self.journal.transition_hunk(
            record.hunk_id,
            expected_version=record.version,
            status=status,
            new_range=new_range or record.new_range,
            post_hash=post_hash or record.post_hash,
            expected_digest=expected_digest or record.expected_digest,
            child_transaction_id=child_transaction_id,
        )


__all__ = [
    "ACCEPTED",
    "AGENT",
    "EXTERNAL",
    "PENDING",
    "REJECTED",
    "REJECTING",
    "ReviewService",
]
