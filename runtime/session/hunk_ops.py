"""Cross-process-safe accept, reject, and undo over durable hunk review facts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Optional

from mote.contracts.file import (
    FileSnapshot,
    HunkRecord,
    MutationResult,
    MutationSet,
    ReviewConflictError,
    TransactionStatus,
)
from mote.runtime.fileops import decode_text, editable_text
from mote.runtime.fileops.hunks import Hunk, HunkApplyError, revert_hunk, slice_lines
from mote.runtime.fileops.mutation import ArtifactWriteScope, ArtifactWriteScopeState, FileMutationArtifactRepository
from mote.runtime.fileops.mutation_factory import MutationFactory
from mote.runtime.fileops.resource_limits import ARTIFACT_WRITE_TTL_SECONDS
from mote.runtime.fileops.review import ACCEPTED, PENDING, REJECTED, REJECTING, ReviewService
from mote.runtime.fileops.transactions import MutationArtifactOwnership, ScopedMutationArtifacts
from mote.runtime.session.hunk_rehydrate import blob_text

__all__ = ["HunkOps", "HunkOpResult", "BatchOpResult"]


@dataclass(frozen=True)
class HunkOpResult:
    hunk_id: str
    ok: bool
    status: str = ""
    error: str = ""


@dataclass(frozen=True)
class BatchOpResult:
    results: list[HunkOpResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def succeeded(self) -> list[HunkOpResult]:
        return [result for result in self.results if result.ok]

    @property
    def failed(self) -> list[HunkOpResult]:
        return [result for result in self.results if not result.ok]


@dataclass(frozen=True)
class _PlannedFileReject:
    path: str
    snapshot: FileSnapshot
    replacement: bytes
    reverted: str
    records: tuple[tuple[HunkRecord, Hunk], ...]


class HunkOps:
    """Coordinates review CAS with the same file locks and child transactions."""

    def __init__(
        self,
        review: ReviewService,
        blobs: FileMutationArtifactRepository,
        *,
        capture_snapshot: Callable[[str], tuple[FileSnapshot, bytes]],
        mutation_factory: MutationFactory,
        commit_mutation_set: Callable[
            [MutationSet, MutationArtifactOwnership],
            MutationResult,
        ],
        resource_lease: Callable[[tuple[FileSnapshot, ...]], object],
    ) -> None:
        self._review = review
        self._blobs = blobs
        self._capture_snapshot = capture_snapshot
        self._mutation_factory = mutation_factory
        self._commit_mutation_set = commit_mutation_set
        self._resource_lease = resource_lease
        self.reconcile_pending()

    def reconcile_pending(self) -> None:
        for record in self._review.records():
            if record.status == REJECTING:
                self._reconcile_rejecting(record)

    def accept(self, hunk_id: str) -> HunkOpResult:
        self._settle(hunk_id)
        record = self._review.status(hunk_id)
        error = self._pending_error(record)
        if error:
            return HunkOpResult(hunk_id, False, error=error)
        snapshot, _, _, _ = self._current(record.path)
        with self._resource_lease((snapshot,)):
            record = self._review.status(hunk_id)
            error = self._pending_error(record, conflict=True)
            if error:
                return HunkOpResult(hunk_id, False, error=error)
            snapshot, _, current, _ = self._current(record.path)
            if not self._matches_expected(record, snapshot, current):
                return HunkOpResult(hunk_id, False, error="drifted")
            try:
                self._review.transition(record, status=ACCEPTED)
            except ReviewConflictError:
                return HunkOpResult(hunk_id, False, error="review conflict")
        return HunkOpResult(hunk_id, True, status=ACCEPTED.value)

    def reject(self, hunk_id: str) -> HunkOpResult:
        self._settle(hunk_id)
        record = self._review.status(hunk_id)
        error = self._pending_error(record)
        if error:
            return HunkOpResult(hunk_id, False, error=error)
        batch = self._reject_batch([record])
        if not batch.results:
            return HunkOpResult(hunk_id, False, error="review conflict")
        return batch.results[0]

    undo = reject

    def accept_file(self, path: str) -> BatchOpResult:
        return BatchOpResult([self.accept(record.hunk_id) for record in self._pending_for(path=path)])

    def reject_file(self, path: str) -> BatchOpResult:
        return self._reject_batch(self._pending_for(path=path))

    def accept_turn(self, turn_index: int) -> BatchOpResult:
        return BatchOpResult([self.accept(record.hunk_id) for record in self._pending_for(turn_index=turn_index)])

    def reject_turn(self, turn_index: int) -> BatchOpResult:
        return self._reject_batch(self._pending_for(turn_index=turn_index))

    def accept_all(self) -> BatchOpResult:
        return BatchOpResult([self.accept(record.hunk_id) for record in self._review.pending()])

    def reject_all(self) -> BatchOpResult:
        return self._reject_batch(self._review.pending())

    def _pending_for(
        self,
        *,
        path: Optional[str] = None,
        turn_index: Optional[int] = None,
    ) -> list[HunkRecord]:
        return [
            record
            for record in self._review.pending()
            if (path is None or record.path == path) and (turn_index is None or record.turn_index == turn_index)
        ]

    def _reject_batch(self, records: list[HunkRecord]) -> BatchOpResult:
        if not records:
            return BatchOpResult()
        ordered = tuple(sorted(records, key=lambda record: (record.path, record.hunk_id)))
        initial: dict[str, FileSnapshot] = {}
        try:
            for path in sorted({record.path for record in ordered}):
                initial[path] = self._current(path)[0]
        except Exception as exc:
            return self._batch_error(ordered, f"read failed: {exc}")

        with self._resource_lease(tuple(initial[path] for path in sorted(initial))):
            latest: list[HunkRecord] = []
            for record in ordered:
                current_record = self._review.status(record.hunk_id)
                error = self._pending_error(current_record, conflict=True)
                if error:
                    return self._batch_error(ordered, error)
                latest.append(current_record)
            try:
                plans = self._plan_rejections(latest)
            except HunkApplyError as exc:
                return self._batch_error(ordered, f"drifted: {exc}")
            except Exception as exc:
                return self._batch_error(ordered, f"read failed: {exc}")

            scope = self._blobs.write_scope(
                owner="hunk-reject",
                maximum_bytes=sum(len(plan.replacement) for plan in plans),
                ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
            )
            with scope:
                mutation_set = self._mutation_factory.mutation_set(
                    source="hunk_reject",
                    mutations=tuple(
                        self._mutation_factory.replacement(
                            plan.snapshot,
                            plan.replacement,
                            scope=scope,
                        )
                        for plan in plans
                    ),
                )
                transaction_id = mutation_set.transaction_id
                rejecting: list[HunkRecord] = []
                try:
                    for record in latest:
                        rejecting.append(
                            self._review.transition(
                                record,
                                status=REJECTING,
                                child_transaction_id=transaction_id,
                            )
                        )
                except ReviewConflictError:
                    self._reset_rejecting(rejecting)
                    scope.discard()
                    return self._batch_error(ordered, "review conflict")

                try:
                    result = self._commit_mutation_set(
                        mutation_set,
                        ScopedMutationArtifacts(scope),
                    )
                    versions = result.versions
                except Exception as exc:
                    transaction = self._review.transaction(transaction_id)
                    if transaction is None or transaction.status == TransactionStatus.ABORTED:
                        self._reset_rejecting(rejecting)
                        if scope.state == ArtifactWriteScopeState.ACTIVE:
                            scope.discard()
                        return self._batch_error(ordered, f"write failed: {exc}")
                    if transaction.status != TransactionStatus.COMMITTED:
                        if scope.state == ArtifactWriteScopeState.ACTIVE:
                            scope.discard()
                        return self._batch_error(ordered, f"write in doubt: {exc}")
                    versions = transaction.committed_versions

                digest_by_path = self._digest_by_path(mutation_set, versions)
                for plan in plans:
                    expected_digest = digest_by_path[plan.path]
                    encoded = plan.reverted.encode("utf-8")
                    review_scope = self._blobs.write_scope(
                        owner="hunk-reject-transition",
                        maximum_bytes=len(encoded),
                        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
                    )
                    with review_scope:
                        post_hash = review_scope.put_bytes(encoded).digest
                        for record, hunk in plan.records:
                            self._shift_pending(
                                plan.path,
                                excluded=record.hunk_id,
                                threshold=hunk.new_start,
                                delta=hunk.old_count - hunk.new_count,
                                post_hash=post_hash,
                                expected_digest=expected_digest,
                                scope=review_scope,
                            )
                        for record, _ in plan.records:
                            current_record = self._review.status(record.hunk_id)
                            if current_record is None or current_record.status != REJECTING:
                                if review_scope.state == ArtifactWriteScopeState.ACTIVE:
                                    review_scope.discard()
                                return self._batch_error(ordered, "review conflict")
                            self._review.transition(
                                current_record,
                                status=REJECTED,
                                post_hash=post_hash,
                                expected_digest=expected_digest,
                                child_transaction_id=transaction_id,
                            )
                            self._complete_review_scope(review_scope)
        return BatchOpResult([HunkOpResult(record.hunk_id, True, status=REJECTED.value) for record in ordered])

    def _hunk(self, record: HunkRecord) -> Hunk:
        old = blob_text(self._blobs, record.pre_hash)
        post = blob_text(self._blobs, record.post_hash)
        return Hunk(
            old_start=record.old_range[0],
            old_count=record.old_range[1],
            new_start=record.new_range[0],
            new_count=record.new_range[1],
            old_text=slice_lines(old, *record.old_range) or None,
            new_text=slice_lines(post, *record.new_range),
        )

    def _current(self, path: str):
        snapshot, raw = self._capture_snapshot(path)
        text, decision = decode_text(raw)
        current = text.replace("\r\n", "\n").replace("\r", "\n")
        return snapshot, raw, current, decision

    def _plan_rejections(
        self,
        records: list[HunkRecord],
    ) -> tuple[_PlannedFileReject, ...]:
        by_path: dict[str, list[HunkRecord]] = {}
        for record in records:
            by_path.setdefault(record.path, []).append(record)
        plans = []
        for path in sorted(by_path):
            snapshot, raw, current, decision = self._current(path)
            selected = tuple(
                sorted(
                    ((record, self._hunk(record)) for record in by_path[path]),
                    key=lambda item: (
                        item[1].new_start,
                        item[1].old_start,
                        item[0].hunk_id,
                    ),
                    reverse=True,
                )
            )
            for record, _ in selected:
                if not self._matches_expected(record, snapshot, current):
                    raise HunkApplyError(f"{record.hunk_id} no longer matches")
            replacement, reverted = self._revert_raw(raw, decision, current, selected)
            plans.append(
                _PlannedFileReject(
                    path=path,
                    snapshot=snapshot,
                    replacement=replacement,
                    reverted=reverted,
                    records=selected,
                )
            )
        return tuple(plans)

    def _matches_expected(
        self,
        record: HunkRecord,
        snapshot: FileSnapshot,
        current: str,
    ) -> bool:
        if record.expected_digest:
            return snapshot.version.digest == record.expected_digest
        return hashlib.sha256(current.encode("utf-8")).hexdigest() == record.post_hash

    @classmethod
    def _revert_raw(
        cls,
        raw: bytes,
        decision,
        current: str,
        selected: tuple[tuple[HunkRecord, Hunk], ...],
    ) -> tuple[bytes, str]:
        editable = editable_text(raw, decision)
        normalized, original_boundaries = cls._normalized_boundaries(editable.text)
        if normalized != current:
            raise HunkApplyError("sealed text normalization is inconsistent")
        replacement = raw
        reverted = current
        for _, hunk in selected:
            logical_start, logical_end = cls._line_span(
                current,
                hunk.new_start,
                hunk.new_count,
            )
            if current[logical_start:logical_end] != hunk.new_text:
                raise HunkApplyError(f"cannot revert hunk at line {hunk.new_start}: content drifted")
            raw_start = editable.logical_to_raw_boundaries[original_boundaries[logical_start]]
            raw_end = editable.logical_to_raw_boundaries[original_boundaries[logical_end]]
            fragment = hunk.old_text or ""
            newline = editable.newline_profile.dominant
            if newline != "\n":
                fragment = fragment.replace("\n", newline)
            encoded = fragment.encode(decision.label, errors="strict")
            replacement = replacement[:raw_start] + encoded + replacement[raw_end:]
            reverted = revert_hunk(reverted, hunk)
        return replacement, reverted

    @staticmethod
    def _normalized_boundaries(text: str) -> tuple[str, tuple[int, ...]]:
        normalized: list[str] = []
        boundaries = [0]
        cursor = 0
        while cursor < len(text):
            if text.startswith("\r\n", cursor):
                normalized.append("\n")
                cursor += 2
            elif text[cursor] == "\r":
                normalized.append("\n")
                cursor += 1
            else:
                normalized.append(text[cursor])
                cursor += 1
            boundaries.append(cursor)
        return "".join(normalized), tuple(boundaries)

    @staticmethod
    def _line_span(content: str, start_line: int, count: int) -> tuple[int, int]:
        lines = content.splitlines(keepends=True)
        start_index = min(max(start_line - 1, 0), len(lines))
        end_index = min(start_index + count, len(lines))
        start = sum(len(line) for line in lines[:start_index])
        end = start + sum(len(line) for line in lines[start_index:end_index])
        return start, end

    @staticmethod
    def _digest_by_path(mutation_set, versions) -> dict[str, str]:
        digests = {}
        for mutation, version in zip(
            mutation_set.mutations,
            versions,
            strict=True,
        ):
            digest = getattr(version, "digest", "")
            if not digest:
                raise ValueError("hunk reject committed a non-present version")
            digests[mutation.requested_path.display] = digest
        return digests

    def _reset_rejecting(self, records: list[HunkRecord]) -> None:
        for record in records:
            latest = self._review.status(record.hunk_id)
            if latest is None or latest.status != REJECTING:
                continue
            try:
                self._review.transition(
                    latest,
                    status=PENDING,
                    child_transaction_id="",
                )
            except ReviewConflictError:
                continue

    @staticmethod
    def _batch_error(
        records: tuple[HunkRecord, ...],
        error: str,
    ) -> BatchOpResult:
        return BatchOpResult([HunkOpResult(record.hunk_id, False, error=error) for record in records])

    def _shift_pending(
        self,
        path: str,
        *,
        excluded: str,
        threshold: int,
        delta: int,
        post_hash: str,
        expected_digest: str,
        scope: ArtifactWriteScope,
    ) -> None:
        for record in self._review.for_path(path):
            if record.hunk_id == excluded or record.status != PENDING:
                continue
            new_range = record.new_range
            if delta and record.new_range[0] > threshold:
                new_range = (record.new_range[0] + delta, record.new_range[1])
            self._review.transition(
                record,
                status=PENDING,
                new_range=new_range,
                post_hash=post_hash,
                expected_digest=expected_digest,
            )
            self._complete_review_scope(scope)

    def _complete_review_scope(self, scope: ArtifactWriteScope) -> None:
        if scope.state == ArtifactWriteScopeState.ACTIVE:
            scope.complete(durability_root=self._review.journal.path.parent)

    def _settle(self, hunk_id: str) -> None:
        record = self._review.status(hunk_id)
        if record is not None and record.status == REJECTING:
            self._reconcile_rejecting(record)

    def _reconcile_rejecting(self, record: HunkRecord) -> None:
        try:
            snapshot, _, current, _ = self._current(record.path)
        except OSError:
            return
        transaction = self._review.transaction(record.child_transaction_id)
        if transaction is None or transaction.status == TransactionStatus.ABORTED:
            try:
                self._review.transition(
                    record,
                    status=PENDING,
                    child_transaction_id="",
                )
            except ReviewConflictError:
                pass
            return
        if transaction.status != TransactionStatus.COMMITTED or not transaction.committed_versions:
            return
        expected_digest = self._digest_by_path(
            transaction.mutation_set,
            transaction.committed_versions,
        ).get(record.path, "")
        if not expected_digest:
            return
        with self._resource_lease((snapshot,)):
            latest = self._review.status(record.hunk_id)
            if latest is None or latest.status != REJECTING:
                return
            snapshot, _, current, _ = self._current(record.path)
            if snapshot.version.digest != expected_digest:
                return
            hunk = self._hunk(latest)
            encoded = current.encode("utf-8")
            scope = self._blobs.write_scope(
                owner="hunk-reject-recovery",
                maximum_bytes=len(encoded),
                ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
            )
            with scope:
                post_hash = scope.put_bytes(encoded).digest
                self._shift_pending(
                    latest.path,
                    excluded=latest.hunk_id,
                    threshold=hunk.new_start,
                    delta=hunk.old_count - hunk.new_count,
                    post_hash=post_hash,
                    expected_digest=expected_digest,
                    scope=scope,
                )
                self._review.transition(
                    latest,
                    status=REJECTED,
                    post_hash=post_hash,
                    expected_digest=expected_digest,
                    child_transaction_id=latest.child_transaction_id,
                )
                self._complete_review_scope(scope)

    @staticmethod
    def _pending_error(record: HunkRecord | None, *, conflict: bool = False) -> str:
        if record is None:
            return "review conflict" if conflict else "unknown"
        if record.status != PENDING:
            return "review conflict" if conflict else "not pending"
        return ""
