"""Crash-durable bounded store for Agent turn acceptance facts."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator

from mote.contracts.agent.capacity import TurnCapacityPermitReceipt
from mote.contracts.clock import AbsoluteInstant
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.contracts.runtime.errors import LeaseCoordinatorUnavailableError, LeaseFencedError
from mote.orchestration.agents.turn_queue.codec import TurnQueueSnapshot, decode_turn_queue, encode_turn_queue
from mote.orchestration.agents.turn_queue.model import (
    TurnAcceptanceRequest,
    TurnAdmissionDisposition,
    TurnAdmissionReceipt,
    TurnClaimBinding,
    TurnMutationDisposition,
    TurnMutationReceipt,
    TurnQueueItem,
    TurnQueueState,
    TurnSchedulingState,
)
from mote.runtime.persistence import disk_io


class TurnQueueStoreError(RuntimeError):
    """The durable queue could not be read, locked, or committed safely."""


@dataclass(frozen=True, slots=True)
class TurnClaimCommit:
    receipt: TurnMutationReceipt
    item: TurnQueueItem | None


class DurableTurnQueueStore:
    def __init__(
        self,
        path: Path,
        *,
        queue_id: str,
        capacity: int,
        lease_coordinator: LeaseCoordinator,
    ) -> None:
        if type(queue_id) is not str or not queue_id:
            raise ValueError("turn queue store identity is invalid")
        if type(capacity) is not int or capacity < 1:
            raise ValueError("turn queue store capacity is invalid")
        self._path = path
        self._lock_path = path.with_name(f"{path.name}.lock")
        self._queue_id = queue_id
        self._capacity = capacity
        self._lease_coordinator = lease_coordinator

    def load(self) -> TurnQueueSnapshot:
        with self._locked():
            return self._read()

    def prepare_acceptance(self, request: TurnAcceptanceRequest, *, lease: LeaseEpoch) -> TurnAdmissionReceipt:
        if request.identity.queue_id != self._queue_id:
            raise ValueError("turn acceptance belongs to another queue")
        with self._locked():
            try:
                self._lease_coordinator.assert_current(lease.subject, lease.fencing_token)
            except LeaseFencedError:
                return TurnAdmissionReceipt(
                    TurnAdmissionDisposition.STALE_FENCE, request.identity.request_id, self._queue_id, None
                )
            except LeaseCoordinatorUnavailableError:
                return TurnAdmissionReceipt(
                    TurnAdmissionDisposition.OWNER_LOST, request.identity.request_id, self._queue_id, None
                )
            snapshot = self._read()
            existing = next(
                (item for item in snapshot.items if item.identity.request_id == request.identity.request_id),
                None,
            )
            if existing is not None:
                disposition = (
                    TurnAdmissionDisposition.DUPLICATE
                    if _matches_acceptance(existing, request)
                    else TurnAdmissionDisposition.CONFLICT
                )
                return TurnAdmissionReceipt(disposition, request.identity.request_id, self._queue_id, existing.revision)
            active = sum(not item.state.terminal for item in snapshot.items)
            if active >= self._capacity:
                return TurnAdmissionReceipt(
                    TurnAdmissionDisposition.REJECTED_CAPACITY,
                    request.identity.request_id,
                    self._queue_id,
                    None,
                )
            item = TurnQueueItem(
                identity=request.identity,
                enqueue_sequence=snapshot.next_enqueue_sequence,
                config_generation=request.config_generation,
                revision=1,
                priority=request.priority,
                state=TurnQueueState.PREPARED,
                accepted_at=request.accepted_at,
                deadline=request.deadline,
                attempt=0,
                maximum_attempts=request.maximum_attempts,
                next_eligible_at=None,
                payload_digest=request.payload_digest,
            )
            committed = TurnQueueSnapshot(
                queue_id=self._queue_id,
                revision=snapshot.revision + 1,
                next_enqueue_sequence=snapshot.next_enqueue_sequence + 1,
                capacity=self._capacity,
                items=(*snapshot.items, item),
                scheduling=snapshot.scheduling,
            )
            self._write(committed)
            return TurnAdmissionReceipt(
                TurnAdmissionDisposition.ACCEPTED,
                request.identity.request_id,
                self._queue_id,
                item.revision,
            )

    def commit_acceptance(
        self, *, request_id: str, expected_item_revision: int, lease: LeaseEpoch
    ) -> TurnMutationReceipt:
        """Publish an eligible turn only after Delivery bound the complete batch."""
        with self._locked():
            fenced = self._fence_receipt(request_id, lease)
            if fenced is not None:
                return fenced
            snapshot = self._read()
            item = next((value for value in snapshot.items if value.identity.request_id == request_id), None)
            if item is None:
                return TurnMutationReceipt(TurnMutationDisposition.NOT_FOUND, self._queue_id, request_id, None, None)
            if item.state is TurnQueueState.ACCEPTED:
                return TurnMutationReceipt(
                    TurnMutationDisposition.APPLIED, self._queue_id, request_id, item.revision, item.state
                )
            if item.state is not TurnQueueState.PREPARED or item.revision != expected_item_revision:
                return TurnMutationReceipt(
                    TurnMutationDisposition.REVISION_CONFLICT,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            committed_item = replace(item, revision=item.revision + 1, state=TurnQueueState.ACCEPTED)
            self._write(
                replace(
                    snapshot,
                    revision=snapshot.revision + 1,
                    items=tuple(committed_item if value is item else value for value in snapshot.items),
                )
            )
            return TurnMutationReceipt(
                TurnMutationDisposition.APPLIED,
                self._queue_id,
                request_id,
                committed_item.revision,
                committed_item.state,
            )

    def claim(
        self,
        *,
        request_id: str,
        expected_queue_revision: int,
        expected_item_revision: int,
        scheduling: TurnSchedulingState,
        lease: LeaseEpoch,
        process_instance_id: str,
        execution_permit_receipt: TurnCapacityPermitReceipt,
        claimed_at: AbsoluteInstant,
    ) -> TurnClaimCommit:
        if not isinstance(claimed_at, AbsoluteInstant):
            raise TypeError("turn claim instant is invalid")
        if type(process_instance_id) is not str or not process_instance_id:
            raise ValueError("turn claim process identity is invalid")
        if not isinstance(execution_permit_receipt, TurnCapacityPermitReceipt):
            raise ValueError("turn claim permit receipt is invalid")
        with self._locked():
            try:
                self._lease_coordinator.assert_current(lease.subject, lease.fencing_token)
            except LeaseFencedError:
                return TurnClaimCommit(
                    TurnMutationReceipt(
                        TurnMutationDisposition.STALE_FENCE,
                        self._queue_id,
                        request_id,
                        None,
                        None,
                    ),
                    None,
                )
            except LeaseCoordinatorUnavailableError:
                return TurnClaimCommit(
                    TurnMutationReceipt(
                        TurnMutationDisposition.OWNER_LOST,
                        self._queue_id,
                        request_id,
                        None,
                        None,
                    ),
                    None,
                )
            snapshot = self._read()
            item = next((value for value in snapshot.items if value.identity.request_id == request_id), None)
            if item is None:
                return TurnClaimCommit(
                    TurnMutationReceipt(TurnMutationDisposition.NOT_FOUND, self._queue_id, request_id, None, None),
                    None,
                )
            if snapshot.revision != expected_queue_revision or item.revision != expected_item_revision:
                return TurnClaimCommit(
                    TurnMutationReceipt(
                        TurnMutationDisposition.REVISION_CONFLICT,
                        self._queue_id,
                        request_id,
                        item.revision,
                        item.state,
                    ),
                    None,
                )
            if item.state.terminal:
                return TurnClaimCommit(
                    TurnMutationReceipt(
                        TurnMutationDisposition.ALREADY_TERMINAL,
                        self._queue_id,
                        request_id,
                        item.revision,
                        item.state,
                    ),
                    None,
                )
            if item.state is not TurnQueueState.ACCEPTED:
                return TurnClaimCommit(
                    TurnMutationReceipt(
                        TurnMutationDisposition.REVISION_CONFLICT,
                        self._queue_id,
                        request_id,
                        item.revision,
                        item.state,
                    ),
                    None,
                )
            new_item_revision = item.revision + 1
            claimed = replace(
                item,
                revision=new_item_revision,
                state=TurnQueueState.CLAIMED,
                attempt=item.attempt + 1,
                next_eligible_at=None,
                claim=TurnClaimBinding(
                    scheduler_subject=lease.subject,
                    scheduler_owner_id=lease.owner_id,
                    scheduler_fencing_token=lease.fencing_token,
                    process_instance_id=process_instance_id,
                    execution_permit_receipt=execution_permit_receipt,
                    queue_revision=new_item_revision,
                    claimed_at=claimed_at,
                ),
            )
            committed = replace(
                snapshot,
                revision=snapshot.revision + 1,
                items=tuple(claimed if value is item else value for value in snapshot.items),
                scheduling=scheduling,
            )
            self._write(committed)
            return TurnClaimCommit(
                TurnMutationReceipt(
                    TurnMutationDisposition.APPLIED,
                    self._queue_id,
                    request_id,
                    claimed.revision,
                    claimed.state,
                ),
                claimed,
            )

    def prepare_execution_settlement(
        self,
        *,
        request_id: str,
        expected_item_revision: int,
        terminal_state: TurnQueueState,
        terminal_reason: str,
        lease: LeaseEpoch,
        process_instance_id: str,
        execution_permit_receipt: TurnCapacityPermitReceipt,
    ) -> TurnMutationReceipt:
        if terminal_state not in {TurnQueueState.SUCCEEDED, TurnQueueState.FAILED}:
            raise ValueError("turn execution settlement state is invalid")
        if type(terminal_reason) is not str or not terminal_reason:
            raise ValueError("turn execution settlement reason is invalid")
        with self._locked():
            fenced = self._fence_receipt(request_id, lease)
            if fenced is not None:
                return fenced
            snapshot = self._read()
            item = next((value for value in snapshot.items if value.identity.request_id == request_id), None)
            if item is None:
                return TurnMutationReceipt(TurnMutationDisposition.NOT_FOUND, self._queue_id, request_id, None, None)
            if item.state.terminal:
                return TurnMutationReceipt(
                    TurnMutationDisposition.ALREADY_TERMINAL,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            claim = item.claim
            if (
                item.state is not TurnQueueState.CLAIMED
                or item.revision != expected_item_revision
                or claim is None
                or claim.scheduler_subject != lease.subject
                or claim.scheduler_owner_id != lease.owner_id
                or claim.scheduler_fencing_token != lease.fencing_token
                or claim.process_instance_id != process_instance_id
                or claim.execution_permit_receipt != execution_permit_receipt
            ):
                return TurnMutationReceipt(
                    TurnMutationDisposition.REVISION_CONFLICT,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            settled = replace(
                item,
                revision=item.revision + 1,
                state=TurnQueueState.EXECUTION_SETTLEMENT_PREPARED,
                claim=replace(claim, queue_revision=item.revision + 1),
                settlement_state=terminal_state,
                terminal_reason=terminal_reason,
            )
            self._write(
                replace(
                    snapshot,
                    revision=snapshot.revision + 1,
                    items=tuple(settled if value is item else value for value in snapshot.items),
                )
            )
            return TurnMutationReceipt(
                TurnMutationDisposition.APPLIED,
                self._queue_id,
                request_id,
                settled.revision,
                settled.state,
            )

    def commit_execution_settlement(
        self,
        *,
        request_id: str,
        expected_item_revision: int,
        lease: LeaseEpoch,
    ) -> TurnMutationReceipt:
        with self._locked():
            fenced = self._fence_receipt(request_id, lease)
            if fenced is not None:
                return fenced
            snapshot = self._read()
            item = next((value for value in snapshot.items if value.identity.request_id == request_id), None)
            if item is None:
                return TurnMutationReceipt(TurnMutationDisposition.NOT_FOUND, self._queue_id, request_id, None, None)
            if item.state.terminal:
                return TurnMutationReceipt(
                    TurnMutationDisposition.ALREADY_TERMINAL,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            if (
                item.state is not TurnQueueState.EXECUTION_SETTLEMENT_PREPARED
                or item.revision != expected_item_revision
                or item.settlement_state not in {TurnQueueState.SUCCEEDED, TurnQueueState.FAILED}
            ):
                return TurnMutationReceipt(
                    TurnMutationDisposition.REVISION_CONFLICT,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            terminal = replace(
                item,
                revision=item.revision + 1,
                state=item.settlement_state,
                claim=None,
                settlement_state=None,
            )
            self._write(
                replace(
                    snapshot,
                    revision=snapshot.revision + 1,
                    items=tuple(terminal if value is item else value for value in snapshot.items),
                )
            )
            return TurnMutationReceipt(
                TurnMutationDisposition.APPLIED,
                self._queue_id,
                request_id,
                terminal.revision,
                terminal.state,
            )

    def settle_unclaimed(
        self,
        *,
        request_id: str,
        expected_item_revision: int,
        terminal_state: TurnQueueState,
        terminal_reason: str,
        lease: LeaseEpoch,
        now: AbsoluteInstant | None = None,
    ) -> TurnMutationReceipt:
        if terminal_state not in {TurnQueueState.CANCELLED, TurnQueueState.EXPIRED}:
            raise ValueError("unclaimed turn settlement state is invalid")
        if type(terminal_reason) is not str or not terminal_reason:
            raise ValueError("turn settlement reason is invalid")
        with self._locked():
            fenced = self._fence_receipt(request_id, lease)
            if fenced is not None:
                return fenced
            snapshot = self._read()
            item = next((value for value in snapshot.items if value.identity.request_id == request_id), None)
            if item is None:
                return TurnMutationReceipt(TurnMutationDisposition.NOT_FOUND, self._queue_id, request_id, None, None)
            if item.state.terminal:
                return TurnMutationReceipt(
                    TurnMutationDisposition.ALREADY_TERMINAL,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            if item.state is not TurnQueueState.ACCEPTED or item.revision != expected_item_revision:
                return TurnMutationReceipt(
                    TurnMutationDisposition.REVISION_CONFLICT,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            if terminal_state is TurnQueueState.EXPIRED:
                if now is None or item.deadline is None or not now.is_at_or_after(item.deadline):
                    return TurnMutationReceipt(
                        TurnMutationDisposition.REVISION_CONFLICT,
                        self._queue_id,
                        request_id,
                        item.revision,
                        item.state,
                    )
            settled = replace(
                item,
                revision=item.revision + 1,
                state=terminal_state,
                terminal_reason=terminal_reason,
            )
            self._write(
                replace(
                    snapshot,
                    revision=snapshot.revision + 1,
                    items=tuple(settled if value is item else value for value in snapshot.items),
                )
            )
            return TurnMutationReceipt(
                TurnMutationDisposition.APPLIED,
                self._queue_id,
                request_id,
                settled.revision,
                settled.state,
            )

    def retry_claim(
        self,
        *,
        request_id: str,
        expected_item_revision: int,
        terminal_reason: str,
        next_eligible_at: AbsoluteInstant,
        lease: LeaseEpoch,
        process_instance_id: str,
        execution_permit_receipt: TurnCapacityPermitReceipt,
    ) -> TurnMutationReceipt:
        if type(terminal_reason) is not str or not terminal_reason:
            raise ValueError("turn retry reason is invalid")
        with self._locked():
            fenced = self._fence_receipt(request_id, lease)
            if fenced is not None:
                return fenced
            snapshot = self._read()
            item = next((value for value in snapshot.items if value.identity.request_id == request_id), None)
            if item is None:
                return TurnMutationReceipt(TurnMutationDisposition.NOT_FOUND, self._queue_id, request_id, None, None)
            if item.state.terminal:
                return TurnMutationReceipt(
                    TurnMutationDisposition.ALREADY_TERMINAL,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            claim = item.claim
            if (
                item.state is not TurnQueueState.CLAIMED
                or item.revision != expected_item_revision
                or claim is None
                or claim.scheduler_subject != lease.subject
                or claim.scheduler_owner_id != lease.owner_id
                or claim.scheduler_fencing_token != lease.fencing_token
                or claim.process_instance_id != process_instance_id
                or claim.execution_permit_receipt != execution_permit_receipt
            ):
                return TurnMutationReceipt(
                    TurnMutationDisposition.REVISION_CONFLICT,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            exhausted = item.attempt >= item.maximum_attempts
            retried = replace(
                item,
                revision=item.revision + 1,
                state=(TurnQueueState.RETRY_EXHAUSTED if exhausted else TurnQueueState.ACCEPTED),
                next_eligible_at=None if exhausted else next_eligible_at,
                claim=None,
                terminal_reason=terminal_reason if exhausted else None,
            )
            self._write(
                replace(
                    snapshot,
                    revision=snapshot.revision + 1,
                    items=tuple(retried if value is item else value for value in snapshot.items),
                )
            )
            return TurnMutationReceipt(
                TurnMutationDisposition.APPLIED,
                self._queue_id,
                request_id,
                retried.revision,
                retried.state,
            )

    def settle_lost_claim(
        self,
        *,
        request_id: str,
        expected_item_revision: int,
        prior_scheduler_fencing_token: int,
        terminal_reason: str,
        lease: LeaseEpoch,
    ) -> TurnMutationReceipt:
        """Settle a claimed turn only after a strictly newer scheduler fence owns recovery."""
        if type(terminal_reason) is not str or not terminal_reason:
            raise ValueError("lost turn settlement reason is invalid")
        with self._locked():
            fenced = self._fence_receipt(request_id, lease)
            if fenced is not None:
                return fenced
            snapshot = self._read()
            item = next((value for value in snapshot.items if value.identity.request_id == request_id), None)
            if item is None:
                return TurnMutationReceipt(TurnMutationDisposition.NOT_FOUND, self._queue_id, request_id, None, None)
            if item.state.terminal:
                return TurnMutationReceipt(
                    TurnMutationDisposition.ALREADY_TERMINAL,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            claim = item.claim
            if (
                item.state is not TurnQueueState.CLAIMED
                or item.revision != expected_item_revision
                or claim is None
                or claim.scheduler_fencing_token != prior_scheduler_fencing_token
                or lease.subject != claim.scheduler_subject
                or lease.fencing_token <= claim.scheduler_fencing_token
            ):
                return TurnMutationReceipt(
                    TurnMutationDisposition.REVISION_CONFLICT,
                    self._queue_id,
                    request_id,
                    item.revision,
                    item.state,
                )
            settled = replace(
                item,
                revision=item.revision + 1,
                state=TurnQueueState.FAILED,
                claim=None,
                terminal_reason=terminal_reason,
            )
            self._write(
                replace(
                    snapshot,
                    revision=snapshot.revision + 1,
                    items=tuple(settled if value is item else value for value in snapshot.items),
                )
            )
            return TurnMutationReceipt(
                TurnMutationDisposition.APPLIED,
                self._queue_id,
                request_id,
                settled.revision,
                settled.state,
            )

    def _fence_receipt(self, request_id: str, lease: LeaseEpoch) -> TurnMutationReceipt | None:
        try:
            self._lease_coordinator.assert_current(lease.subject, lease.fencing_token)
        except LeaseFencedError:
            return TurnMutationReceipt(
                TurnMutationDisposition.STALE_FENCE,
                self._queue_id,
                request_id,
                None,
                None,
            )
        except LeaseCoordinatorUnavailableError:
            return TurnMutationReceipt(
                TurnMutationDisposition.OWNER_LOST,
                self._queue_id,
                request_id,
                None,
                None,
            )
        return None

    def _read(self) -> TurnQueueSnapshot:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return TurnQueueSnapshot(self._queue_id, 0, 1, self._capacity, ())
        except (OSError, json.JSONDecodeError) as exc:
            raise TurnQueueStoreError("durable turn queue state is unreadable") from exc
        try:
            snapshot = decode_turn_queue(raw, expected_queue_id=self._queue_id)
        except (TypeError, ValueError) as exc:
            raise TurnQueueStoreError("durable turn queue state is invalid") from exc
        if snapshot.capacity != self._capacity:
            raise TurnQueueStoreError("durable turn queue capacity does not match composition")
        return snapshot

    def _write(self, snapshot: TurnQueueSnapshot) -> None:
        try:
            payload = json.dumps(encode_turn_queue(snapshot), sort_keys=True, separators=(",", ":")).encode("utf-8")
            disk_io.atomic_write(self._path, payload, fsync=True)
        except (OSError, TypeError, ValueError) as exc:
            raise TurnQueueStoreError("durable turn queue commit failed") from exc

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self._lock_path.open("a+b")
        except OSError as exc:
            raise TurnQueueStoreError("durable turn queue lock cannot be opened") from exc
        with lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except TurnQueueStoreError:
                raise
            except OSError as exc:
                raise TurnQueueStoreError("durable turn queue lock failed") from exc


def _matches_acceptance(item: TurnQueueItem, request: TurnAcceptanceRequest) -> bool:
    return (
        item.identity == request.identity
        and item.config_generation == request.config_generation
        and item.priority is request.priority
        and item.accepted_at == request.accepted_at
        and item.deadline == request.deadline
        and item.maximum_attempts == request.maximum_attempts
        and item.payload_digest == request.payload_digest
    )


__all__ = ["DurableTurnQueueStore", "TurnClaimCommit", "TurnQueueStoreError"]
