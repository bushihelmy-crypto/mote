"""Fenced durable Agent lineage projection and spawn state machine."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from mote.contracts.agent.budget import AgentBudgetDisposition, AgentBudgetReservationReceipt
from mote.contracts.agent.capacity import CapacityReservationDisposition, LogicalCapacityReservationReceipt
from mote.contracts.agent.lineage import (
    LineageAuthorizationDisposition,
    LineageAuthorizationReceipt,
    LineageRecord,
    SpawnAdvanceDisposition,
    SpawnAdvanceReceipt,
    SpawnLifecycle,
    SpawnRequest,
    SubtreeLineageSnapshot,
)
from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, LineageRevision
from mote.contracts.file.identity import LockMode, LockSpec
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.contracts.runtime.operation_ownership import OperationOwnership
from mote.contracts.workflow import (
    WorkflowCreateAdmission,
    WorkflowCreateAdmissionDisposition,
    WorkflowCreateAdmissionId,
    WorkflowCreateAdmissionLifecycle,
    WorkflowCreateAdmissionReceipt,
    WorkflowRunReference,
)
from mote.orchestration.agents.lineage.codec import decode_lineage, encode_lineage
from mote.runtime.fileops.locking import PROJECT_LOCK_LEVEL, HierarchicalLockManager
from mote.runtime.persistence.atomic import atomic_write

_NEXT: dict[SpawnLifecycle, frozenset[SpawnLifecycle]] = {
    SpawnLifecycle.REQUESTED: frozenset({SpawnLifecycle.ADMITTED, SpawnLifecycle.REJECTED, SpawnLifecycle.ABORTED}),
    SpawnLifecycle.ADMITTED: frozenset(
        {SpawnLifecycle.LINEAGE_COMMITTED, SpawnLifecycle.REJECTED, SpawnLifecycle.ABORTED}
    ),
    SpawnLifecycle.LINEAGE_COMMITTED: frozenset({SpawnLifecycle.PLACEMENT_PENDING, SpawnLifecycle.ABORTED}),
    SpawnLifecycle.PLACEMENT_PENDING: frozenset({SpawnLifecycle.INCARNATION_STARTED, SpawnLifecycle.ABORTED}),
    SpawnLifecycle.INCARNATION_STARTED: frozenset(
        {SpawnLifecycle.ACTIVE, SpawnLifecycle.PLACEMENT_PENDING, SpawnLifecycle.ABORTED}
    ),
    SpawnLifecycle.ACTIVE: frozenset({SpawnLifecycle.PLACEMENT_PENDING, SpawnLifecycle.TERMINAL}),
    SpawnLifecycle.REJECTED: frozenset(),
    SpawnLifecycle.ABORTED: frozenset(),
    SpawnLifecycle.TERMINAL: frozenset(),
}


class AgentLineageStore:
    """One revisioned lineage truth, persisted by atomic replacement."""

    def __init__(self, path: Path, *, lease_coordinator: LeaseCoordinator) -> None:
        self._path = Path(path)
        self._coordinator = lease_coordinator
        self._locks = HierarchicalLockManager(self._path.parent / ".lineage-locks")

    def register_root(self, agent_id: str, definition_id: str, *, lease: LeaseEpoch) -> LineageRecord:
        request = SpawnRequest(
            f"root:{agent_id}",
            agent_id,
            None,
            "/root",
            None,
            definition_id,
            f"root-capacity:{agent_id}",
            (),
        )
        with self._mutation(lease):
            revision, records, admissions = self._read()
            prior = self._by_request(records).get(request.request_id)
            if prior is not None:
                if prior.request != request or prior.logical_agent_id != agent_id:
                    raise ValueError("root lineage identity conflicts with durable root")
                return prior
            if records:
                roots = [record for record in records if record.request.parent_agent_id is None]
                if roots and roots[0].logical_agent_id != agent_id:
                    raise ValueError("lineage already has a different root")
            record = LineageRecord(
                request,
                agent_id,
                SpawnLifecycle.ACTIVE,
                revision + 1,
                revision + 1,
                None,
                1,
                "supervisor",
                lease.fencing_token,
            )
            self._write(records + (record,), admissions, revision + 1)
            return record

    def request_spawn(
        self,
        request: SpawnRequest,
        *,
        capacity: LogicalCapacityReservationReceipt,
        budget: AgentBudgetReservationReceipt | None,
        lease: LeaseEpoch,
    ) -> SpawnAdvanceReceipt:
        if capacity.disposition is not CapacityReservationDisposition.RESERVED:
            raise ValueError("lineage requires a reserved logical capacity receipt")
        if capacity.reservation_id != request.capacity_reservation_id:
            raise ValueError("lineage capacity reservation binding differs")
        budget_ids = () if budget is None else tuple(item.reservation_id for item in budget.reservations)
        if budget is not None and budget.disposition is not AgentBudgetDisposition.RESERVED:
            raise ValueError("lineage requires a reserved budget receipt")
        if budget_ids != request.budget_reservation_ids:
            raise ValueError("lineage budget reservation binding differs")
        with self._mutation(lease):
            revision, records, admissions = self._read()
            by_request = self._by_request(records)
            prior = by_request.get(request.request_id)
            if prior is not None:
                disposition = (
                    SpawnAdvanceDisposition.IDEMPOTENT if prior.request == request else SpawnAdvanceDisposition.CONFLICT
                )
                return SpawnAdvanceReceipt(request.request_id, disposition, prior)
            live = tuple(record for record in records if not record.tombstoned)
            if any(record.request.agent_path == request.agent_path for record in live):
                return SpawnAdvanceReceipt(request.request_id, SpawnAdvanceDisposition.CONFLICT, None)
            if request.nickname is not None and any(record.request.nickname == request.nickname for record in live):
                return SpawnAdvanceReceipt(request.request_id, SpawnAdvanceDisposition.CONFLICT, None)
            if request.parent_agent_id is not None:
                parent = self._by_agent(live).get(request.parent_agent_id)
                if parent is None or parent.lifecycle not in {
                    SpawnLifecycle.ACTIVE,
                    SpawnLifecycle.INCARNATION_STARTED,
                }:
                    return SpawnAdvanceReceipt(request.request_id, SpawnAdvanceDisposition.CONFLICT, None)
                if parent.request.root_agent_id != request.root_agent_id:
                    return SpawnAdvanceReceipt(request.request_id, SpawnAdvanceDisposition.CONFLICT, None)
                if not request.agent_path.startswith(parent.request.agent_path.rstrip("/") + "/"):
                    return SpawnAdvanceReceipt(request.request_id, SpawnAdvanceDisposition.CONFLICT, None)
                ancestor_path = parent.request.agent_path
                if any(
                    record.cancellation_epoch > 0
                    and (
                        ancestor_path == record.request.agent_path
                        or ancestor_path.startswith(record.request.agent_path.rstrip("/") + "/")
                    )
                    for record in live
                ):
                    return SpawnAdvanceReceipt(request.request_id, SpawnAdvanceDisposition.CONFLICT, None)
            next_revision = revision + 1
            record = LineageRecord(
                request,
                None,
                SpawnLifecycle.REQUESTED,
                next_revision,
                next_revision,
                next_revision if request.nickname is not None else None,
                0,
                None,
                lease.fencing_token,
            )
            self._write(records + (record,), admissions, next_revision)
            return SpawnAdvanceReceipt(request.request_id, SpawnAdvanceDisposition.APPLIED, record)

    def advance(
        self,
        request_id: str,
        target: SpawnLifecycle,
        *,
        expected_revision: int,
        lease: LeaseEpoch,
        placement: str | None = None,
        incarnation_generation: int | None = None,
    ) -> SpawnAdvanceReceipt:
        with self._mutation(lease):
            projection_revision, records, admissions = self._read()
            current = self._by_request(records).get(request_id)
            if current is None:
                return SpawnAdvanceReceipt(request_id, SpawnAdvanceDisposition.NOT_FOUND, None)
            if current.lifecycle is target:
                return SpawnAdvanceReceipt(request_id, SpawnAdvanceDisposition.IDEMPOTENT, current)
            if current.revision != expected_revision:
                return SpawnAdvanceReceipt(request_id, SpawnAdvanceDisposition.STALE_REVISION, current)
            if target not in _NEXT[current.lifecycle]:
                return SpawnAdvanceReceipt(request_id, SpawnAdvanceDisposition.CONFLICT, current)
            logical_id = current.logical_agent_id
            if target is SpawnLifecycle.LINEAGE_COMMITTED:
                logical_id = uuid.uuid5(uuid.NAMESPACE_URL, f"mote-agent:{request_id}").hex
                if logical_id in self._by_agent(records):
                    return SpawnAdvanceReceipt(request_id, SpawnAdvanceDisposition.CONFLICT, current)
            generation = current.incarnation_generation
            if target is SpawnLifecycle.INCARNATION_STARTED:
                if incarnation_generation is None or incarnation_generation != generation + 1:
                    return SpawnAdvanceReceipt(request_id, SpawnAdvanceDisposition.CONFLICT, current)
                generation = incarnation_generation
            elif incarnation_generation is not None and incarnation_generation != generation:
                return SpawnAdvanceReceipt(request_id, SpawnAdvanceDisposition.CONFLICT, current)
            if target in {SpawnLifecycle.PLACEMENT_PENDING, SpawnLifecycle.INCARNATION_STARTED} and not placement:
                return SpawnAdvanceReceipt(request_id, SpawnAdvanceDisposition.CONFLICT, current)
            next_revision = projection_revision + 1
            updated = replace(
                current,
                logical_agent_id=logical_id,
                lifecycle=target,
                revision=next_revision,
                incarnation_generation=generation,
                placement=placement if placement is not None else current.placement,
                owner_fencing_token=lease.fencing_token,
                tombstoned=target is SpawnLifecycle.TERMINAL,
            )
            next_records = tuple(updated if row.request.request_id == request_id else row for row in records)
            self._write(next_records, admissions, next_revision)
            return SpawnAdvanceReceipt(request_id, SpawnAdvanceDisposition.APPLIED, updated)

    def records(self) -> tuple[LineageRecord, ...]:
        return self._read()[1]

    def reserve_workflow_create(
        self,
        *,
        admission_id: WorkflowCreateAdmissionId,
        create_request_id: str,
        reference: WorkflowRunReference,
        logical_agent_id: str,
        expected_lineage_revision: int,
        cancellation_epoch: int,
        ownership: OperationOwnership,
        lease: LeaseEpoch,
    ) -> WorkflowCreateAdmissionReceipt:
        """Atomically reserve create against lineage cancellation cutoff."""
        with self._mutation(lease):
            revision, records, admissions = self._read()
            prior = next((item for item in admissions if item.admission_id == admission_id), None)
            if prior is not None:
                same = (
                    prior.create_request_id == create_request_id
                    and prior.reference == reference
                    and prior.logical_agent_id == logical_agent_id
                )
                if not same:
                    disposition = WorkflowCreateAdmissionDisposition.IDENTITY_CONFLICT
                elif prior.lifecycle is WorkflowCreateAdmissionLifecycle.ABORTED:
                    disposition = WorkflowCreateAdmissionDisposition.PREVIOUS_ADMISSION_ABORTED
                else:
                    disposition = WorkflowCreateAdmissionDisposition.IDEMPOTENT
                return WorkflowCreateAdmissionReceipt(disposition, prior)
            owner = self._by_agent(records).get(logical_agent_id)
            if (
                owner is None
                or owner.logical_agent_id is None
                or owner.lifecycle is not SpawnLifecycle.ACTIVE
                or owner.revision != expected_lineage_revision
                or owner.cancellation_epoch != cancellation_epoch
            ):
                return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.STALE_REVISION, None)
            next_revision = revision + 1
            admission = WorkflowCreateAdmission(
                admission_id,
                create_request_id,
                reference,
                AgentId(owner.logical_agent_id),
                AgentId(owner.request.root_agent_id),
                LineageRevision(owner.revision),
                CancellationEpoch(owner.cancellation_epoch),
                next_revision,
                ownership,
                WorkflowCreateAdmissionLifecycle.RESERVED,
            )
            self._write(records, admissions + (admission,), next_revision)
            return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.RESERVED, admission)

    def settle_workflow_create(
        self,
        admission_id: WorkflowCreateAdmissionId,
        lifecycle: WorkflowCreateAdmissionLifecycle,
        *,
        expected_revision: int,
        ownership: OperationOwnership,
        lease: LeaseEpoch,
    ) -> WorkflowCreateAdmissionReceipt:
        if lifecycle is WorkflowCreateAdmissionLifecycle.RESERVED:
            raise ValueError("Workflow admission settlement must be terminal")
        with self._mutation(lease):
            revision, records, admissions = self._read()
            current = next((item for item in admissions if item.admission_id == admission_id), None)
            if current is None:
                return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.IDENTITY_CONFLICT, None)
            if current.lifecycle is lifecycle:
                return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.IDEMPOTENT, current)
            if current.lifecycle is WorkflowCreateAdmissionLifecycle.ABORTED:
                return WorkflowCreateAdmissionReceipt(
                    WorkflowCreateAdmissionDisposition.PREVIOUS_ADMISSION_ABORTED, current
                )
            if current.revision != expected_revision:
                return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.STALE_REVISION, current)
            if (
                current.ownership.subject != ownership.subject
                or current.ownership.fencing_token != ownership.fencing_token
            ):
                return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.FENCE_LOST, current)
            next_revision = revision + 1
            updated = replace(current, lifecycle=lifecycle, revision=next_revision)
            next_admissions = tuple(updated if item.admission_id == admission_id else item for item in admissions)
            self._write(records, next_admissions, next_revision)
            return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.SETTLED, updated)

    def claim_workflow_create(
        self,
        admission_id: WorkflowCreateAdmissionId,
        *,
        expected_revision: int,
        ownership: OperationOwnership,
        lease: LeaseEpoch,
    ) -> WorkflowCreateAdmissionReceipt:
        """Transfer a stale RESERVED admission to its fenced reconciler."""
        with self._mutation(lease):
            revision, records, admissions = self._read()
            current = next(
                (item for item in admissions if item.admission_id == admission_id),
                None,
            )
            if current is None:
                return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.IDENTITY_CONFLICT, None)
            if current.lifecycle is not WorkflowCreateAdmissionLifecycle.RESERVED:
                return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.IDEMPOTENT, current)
            if current.revision != expected_revision:
                return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.STALE_REVISION, current)
            if (
                ownership.subject != current.ownership.subject
                or ownership.fencing_token <= current.ownership.fencing_token
                or ownership.request.operation_id != str(admission_id)
            ):
                return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.FENCE_LOST, current)
            next_revision = revision + 1
            updated = replace(current, revision=next_revision, ownership=ownership)
            next_admissions = tuple(updated if item.admission_id == admission_id else item for item in admissions)
            self._write(records, next_admissions, next_revision)
            return WorkflowCreateAdmissionReceipt(WorkflowCreateAdmissionDisposition.CLAIMED, updated)

    def workflow_create_admissions(self) -> tuple[WorkflowCreateAdmission, ...]:
        return self._read()[2]

    def get_workflow_create_admission(self, admission_id: WorkflowCreateAdmissionId) -> WorkflowCreateAdmission | None:
        return next(
            (item for item in self._read()[2] if item.admission_id == admission_id),
            None,
        )

    def record_for_request(self, request_id: str) -> LineageRecord | None:
        return self._by_request(self.records()).get(request_id)

    def record_for_agent(self, agent_id: str) -> LineageRecord | None:
        return self._by_agent(self.records()).get(agent_id)

    def pending_reconciliation(self) -> tuple[LineageRecord, ...]:
        return tuple(
            record
            for record in self.records()
            if record.lifecycle
            in {
                SpawnLifecycle.REQUESTED,
                SpawnLifecycle.ADMITTED,
                SpawnLifecycle.LINEAGE_COMMITTED,
                SpawnLifecycle.PLACEMENT_PENDING,
                SpawnLifecycle.INCARNATION_STARTED,
            }
        )

    def subtree_snapshot(self, agent_id: str) -> SubtreeLineageSnapshot:
        revision, records, admissions = self._read()
        by_agent = self._by_agent(records)
        owner = by_agent.get(agent_id)
        if owner is None:
            raise KeyError(agent_id)
        prefix = owner.request.agent_path.rstrip("/") + "/"
        ids = tuple(
            sorted(
                record.logical_agent_id
                for record in records
                if record.logical_agent_id is not None
                and not record.tombstoned
                and (record.logical_agent_id == agent_id or record.request.agent_path.startswith(prefix))
            )
        )
        return SubtreeLineageSnapshot(
            owner.request.root_agent_id,
            agent_id,
            revision,
            ids,
            owner.cancellation_epoch,
            tuple(sorted(str(item.admission_id) for item in admissions if str(item.logical_agent_id) in ids)),
        )

    def begin_subtree_cancellation(self, agent_id: str, *, lease: LeaseEpoch) -> SubtreeLineageSnapshot:
        with self._mutation(lease):
            projection_revision, records, admissions = self._read()
            owner = self._by_agent(records).get(agent_id)
            if owner is None or owner.tombstoned:
                raise KeyError(agent_id)
            next_revision = projection_revision + 1
            updated = replace(
                owner,
                revision=next_revision,
                cancellation_epoch=owner.cancellation_epoch + 1,
                owner_fencing_token=lease.fencing_token,
            )
            prefix = owner.request.agent_path.rstrip("/") + "/"
            pending = {
                SpawnLifecycle.REQUESTED,
                SpawnLifecycle.ADMITTED,
                SpawnLifecycle.LINEAGE_COMMITTED,
                SpawnLifecycle.PLACEMENT_PENDING,
                SpawnLifecycle.INCARNATION_STARTED,
            }
            next_records = tuple(
                (
                    updated
                    if row.request.request_id == owner.request.request_id
                    else (
                        replace(
                            row,
                            lifecycle=SpawnLifecycle.ABORTED,
                            revision=next_revision,
                            owner_fencing_token=lease.fencing_token,
                        )
                        if row.request.agent_path.startswith(prefix) and row.lifecycle in pending
                        else row
                    )
                )
                for row in records
            )
            self._write(next_records, admissions, next_revision)
            ids = tuple(
                sorted(
                    row.logical_agent_id
                    for row in next_records
                    if row.logical_agent_id is not None
                    and not row.tombstoned
                    and (row.logical_agent_id == agent_id or row.request.agent_path.startswith(prefix))
                )
            )
            return SubtreeLineageSnapshot(
                owner.request.root_agent_id,
                agent_id,
                next_revision,
                ids,
                updated.cancellation_epoch,
                tuple(sorted(str(item.admission_id) for item in admissions if str(item.logical_agent_id) in ids)),
            )

    def cancellation_snapshot(self, agent_id: str, *, cancellation_epoch: int) -> SubtreeLineageSnapshot:
        revision, records, admissions = self._read()
        owner = self._by_agent(records).get(agent_id)
        if owner is None or owner.cancellation_epoch != cancellation_epoch:
            raise ValueError("subtree cancellation epoch is stale")
        prefix = owner.request.agent_path.rstrip("/") + "/"
        ids = tuple(
            sorted(
                row.logical_agent_id
                for row in records
                if row.logical_agent_id is not None
                and (row.logical_agent_id == agent_id or row.request.agent_path.startswith(prefix))
            )
        )
        return SubtreeLineageSnapshot(
            owner.request.root_agent_id,
            agent_id,
            revision,
            ids,
            cancellation_epoch,
            tuple(sorted(str(item.admission_id) for item in admissions if str(item.logical_agent_id) in ids)),
        )

    def authorize_incarnation(
        self, agent_id: str, *, incarnation_generation: int, fencing_token: int
    ) -> LineageAuthorizationReceipt:
        revision, records, _ = self._read()
        record = self._by_agent(records).get(agent_id)
        if record is None:
            disposition = LineageAuthorizationDisposition.NOT_FOUND
        elif record.lifecycle is not SpawnLifecycle.ACTIVE:
            disposition = LineageAuthorizationDisposition.NOT_ACTIVE
        elif record.incarnation_generation != incarnation_generation:
            disposition = LineageAuthorizationDisposition.INCARNATION_MISMATCH
        elif record.owner_fencing_token != fencing_token:
            disposition = LineageAuthorizationDisposition.STALE_FENCE
        else:
            disposition = LineageAuthorizationDisposition.AUTHORIZED
        return LineageAuthorizationReceipt(agent_id, disposition, revision)

    def cancellation_target_is_current(
        self,
        *,
        root_agent_id: str,
        subtree_agent_id: str,
        target_agent_id: str,
        lineage_revision: int,
        cancellation_epoch: int,
    ) -> bool:
        revision, records, _ = self._read()
        if lineage_revision > revision:
            return False
        by_agent = self._by_agent(records)
        subtree = by_agent.get(subtree_agent_id)
        target = by_agent.get(target_agent_id)
        if subtree is None or target is None:
            return False
        prefix = subtree.request.agent_path.rstrip("/") + "/"
        return (
            subtree.request.root_agent_id == root_agent_id
            and subtree.cancellation_epoch == cancellation_epoch
            and (target_agent_id == subtree_agent_id or target.request.agent_path.startswith(prefix))
        )

    def _read(self) -> tuple[int, tuple[LineageRecord, ...], tuple[WorkflowCreateAdmission, ...]]:
        if not self._path.exists():
            return 0, (), ()
        return decode_lineage(self._path.read_bytes())

    def _write(
        self,
        records: tuple[LineageRecord, ...],
        admissions: tuple[WorkflowCreateAdmission, ...],
        revision: int,
    ) -> None:
        atomic_write(self._path, encode_lineage(records, admissions, revision), fsync=True, mode=0o600)

    @contextmanager
    def _mutation(self, lease: LeaseEpoch):
        lock = LockSpec(PROJECT_LOCK_LEVEL, str(self._path), LockMode.EXCLUSIVE, "Agent lineage")
        with self._locks.acquire_many((lock,)):
            with self._coordinator.guard(lease.subject, lease.fencing_token):
                yield

    @staticmethod
    def _by_request(records: tuple[LineageRecord, ...]) -> dict[str, LineageRecord]:
        return {record.request.request_id: record for record in records}

    @staticmethod
    def _by_agent(records: tuple[LineageRecord, ...]) -> dict[str, LineageRecord]:
        return {record.logical_agent_id: record for record in records if record.logical_agent_id is not None}


__all__ = ["AgentLineageStore"]
