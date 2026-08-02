"""Durable Workflow effect and terminal-delivery reconciliation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.contracts.ports.workflow.governance import (
    WorkflowGovernanceAdmissionQueryPort,
    WorkflowGovernanceSnapshotVerifierPort,
)
from mote.contracts.runtime.errors import LeaseCoordinatorUnavailableError, LeaseUnavailableError
from mote.contracts.runtime.operation_ownership import (
    EffectCapability,
    EffectSettlement,
    OperationBackend,
    OperationOwnershipRequest,
    project_operation_guarantee,
)
from mote.contracts.workflow.admission import WorkflowCreateAdmissionLifecycle
from mote.contracts.workflow.governance import (
    WorkflowGovernanceAcceptanceDisposition,
    WorkflowGovernanceCancelAcceptance,
    WorkflowGovernanceCancelRequest,
    WorkflowGovernanceCancelSettlementSnapshot,
    WorkflowGovernanceRunDisposition,
    WorkflowGovernanceRunSettlement,
    WorkflowGovernanceScopeCancelRequestId,
    WorkflowGovernanceSettlementLifecycle,
    WorkflowGovernanceSnapshotVerification,
)
from mote.contracts.workflow.governance_codec import (
    decode_workflow_governance_cancel,
    encode_workflow_governance_cancel,
)
from mote.contracts.workflow.identity import WorkflowDefinitionId, WorkflowRunId, WorkflowRunReference

from .control import WorkflowRunControl
from .model import WorkflowRunCommand, WorkflowRunPhase
from .store import WorkflowRunStore

_SCHEMA = "mote.workflow-reconciliation/v2"


class ReconcileState(str, Enum):
    AVAILABLE = "available"
    CLAIMED = "claimed"
    SETTLED = "settled"
    IN_DOUBT = "in_doubt"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class WorkflowEffect:
    effect_id: str
    run_id: WorkflowRunId
    capability: EffectCapability
    command_payload: str
    provider_receipt: str
    state: ReconcileState
    revision: int
    attempts: int
    next_eligible_at: AbsoluteInstant
    reason: str = ""


@dataclass(frozen=True, slots=True)
class WorkflowTerminalDelivery:
    delivery_id: str
    run_id: WorkflowRunId
    destination_id: str
    outcome_payload: str
    state: ReconcileState
    revision: int
    attempts: int
    next_eligible_at: AbsoluteInstant
    reason: str = ""


@dataclass(frozen=True, slots=True)
class WorkflowGovernanceCancellation:
    request: WorkflowGovernanceCancelRequest
    revision: int
    lifecycle: WorkflowGovernanceSettlementLifecycle
    per_run_settlements: tuple[WorkflowGovernanceRunSettlement, ...]
    attempts: int
    next_eligible_at: AbsoluteInstant
    reason: str = ""


class WorkflowReconciliationStore:
    def __init__(self, path: Path, ownership, *, governance_capacity: int = 1024) -> None:
        if governance_capacity < 1:
            raise ValueError("Workflow governance capacity must be positive")
        self._path = path
        self._ownership = ownership
        self._governance_capacity = governance_capacity

    @staticmethod
    def effect_identity(run_id: WorkflowRunId, logical_key: str) -> str:
        return "wfe_" + hashlib.sha256(f"{run_id}\0{logical_key}".encode()).hexdigest()

    @staticmethod
    def delivery_identity(run_id: WorkflowRunId, destination_id: str) -> str:
        return "wfd_" + hashlib.sha256(f"{run_id}\0{destination_id}".encode()).hexdigest()

    def records(self):
        return self._read()

    def terminal_deliveries_for_run(self, run_id: WorkflowRunId) -> tuple[WorkflowTerminalDelivery, ...]:
        return tuple(
            sorted(
                (item for item in self._read()["deliveries"].values() if item.run_id == run_id),
                key=lambda item: item.destination_id,
            )
        )

    def commit_effect(self, item: WorkflowEffect, expected_revision: int | None, ownership) -> WorkflowEffect:
        return self._commit("effects", item.effect_id, item, expected_revision, ownership)

    def commit_delivery(
        self, item: WorkflowTerminalDelivery, expected_revision: int | None, ownership
    ) -> WorkflowTerminalDelivery:
        return self._commit("deliveries", item.delivery_id, item, expected_revision, ownership)

    def submit_verified(self, request: WorkflowGovernanceCancelRequest) -> WorkflowGovernanceCancelAcceptance:
        identity = str(request.request_id)
        ownership = self._claim_governance(identity, 0)
        try:
            with self._ownership.guard(ownership):
                with self._store_transaction():
                    state = self._read()
                    current = state["governance_cancellations"].get(request.request_id)
                    if current is not None:
                        disposition = (
                            WorkflowGovernanceAcceptanceDisposition.IDEMPOTENT
                            if current.request == request
                            else WorkflowGovernanceAcceptanceDisposition.SCOPE_MISMATCH
                        )
                        return WorkflowGovernanceCancelAcceptance(
                            request.request_id,
                            disposition,
                            current.revision,
                            len(current.request.target_agent_ids),
                        )
                    active = sum(
                        item.lifecycle
                        not in {
                            WorkflowGovernanceSettlementLifecycle.SETTLED,
                            WorkflowGovernanceSettlementLifecycle.DEAD_LETTER,
                        }
                        for item in state["governance_cancellations"].values()
                    )
                    if active >= self._governance_capacity:
                        return WorkflowGovernanceCancelAcceptance(
                            request.request_id,
                            WorkflowGovernanceAcceptanceDisposition.BACKPRESSURED,
                            None,
                            len(request.target_agent_ids),
                        )
                    item = WorkflowGovernanceCancellation(
                        request,
                        1,
                        WorkflowGovernanceSettlementLifecycle.PENDING,
                        (),
                        0,
                        AbsoluteInstant(1, UNIX_UTC_CLOCK, 0),
                    )
                    state["governance_cancellations"][request.request_id] = item
                    self._write(state)
                    return WorkflowGovernanceCancelAcceptance(
                        request.request_id,
                        WorkflowGovernanceAcceptanceDisposition.ACCEPTED,
                        item.revision,
                        len(request.target_agent_ids),
                    )
        except RuntimeError:
            return WorkflowGovernanceCancelAcceptance(
                request.request_id,
                WorkflowGovernanceAcceptanceDisposition.FENCE_LOST,
                None,
                len(request.target_agent_ids),
            )
        finally:
            self._ownership.release(ownership)

    def get(
        self, request_id: WorkflowGovernanceScopeCancelRequestId
    ) -> WorkflowGovernanceCancelSettlementSnapshot | None:
        item = self._read()["governance_cancellations"].get(request_id)
        if item is None:
            return None
        return WorkflowGovernanceCancelSettlementSnapshot(
            item.request.request_id,
            item.revision,
            item.lifecycle,
            item.per_run_settlements,
        )

    def commit_governance(
        self,
        item: WorkflowGovernanceCancellation,
        expected_revision: int,
        ownership,
    ) -> WorkflowGovernanceCancellation:
        return self._commit(
            "governance_cancellations",
            str(item.request.request_id),
            item,
            expected_revision,
            ownership,
        )

    def _claim_governance(self, identity: str, revision: int):
        return self._ownership.claim(
            OperationOwnershipRequest(
                "workflow-governance",
                identity,
                "workflow-governance-delivery",
                OperationBackend.LOCAL_FILE,
                revision,
                identity,
                EffectCapability.IDEMPOTENT_BY_KEY,
            ),
            30.0,
        )

    def _commit(self, collection, identity, item, expected_revision, ownership):
        if ownership.request.operation_id != identity:
            raise RuntimeError("reconciliation ownership identity mismatch")
        with self._ownership.guard(ownership):
            with self._store_transaction():
                state = self._read()
                current = state[collection].get(identity)
                actual = None if current is None else current.revision
                if actual != expected_revision:
                    raise RuntimeError("reconciliation revision conflict")
                state[collection][identity] = item
                self._write(state)
                return item

    @contextmanager
    def _store_transaction(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(f"{self._path.suffix}.lock")
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self):
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"effects": {}, "deliveries": {}, "governance_cancellations": {}}
        if (
            type(raw) is not dict
            or set(raw) != {"schema", "effects", "deliveries", "governance_cancellations"}
            or raw["schema"] != _SCHEMA
        ):
            raise ValueError("workflow reconciliation envelope is invalid")
        effects = {}
        deliveries = {}
        governance_cancellations = {}
        for item in raw["effects"]:
            fields = {
                "effect_id",
                "run_id",
                "capability",
                "command_payload",
                "provider_receipt",
                "state",
                "revision",
                "attempts",
                "next_eligible_at",
                "reason",
            }
            _strict_item(item, fields)
            record = WorkflowEffect(
                item["effect_id"],
                WorkflowRunId(item["run_id"]),
                EffectCapability(item["capability"]),
                item["command_payload"],
                item["provider_receipt"],
                ReconcileState(item["state"]),
                item["revision"],
                item["attempts"],
                AbsoluteInstant.from_dict(item["next_eligible_at"]),
                item["reason"],
            )
            effects[record.effect_id] = record
        for item in raw["deliveries"]:
            fields = {
                "delivery_id",
                "run_id",
                "destination_id",
                "outcome_payload",
                "state",
                "revision",
                "attempts",
                "next_eligible_at",
                "reason",
            }
            _strict_item(item, fields)
            record = WorkflowTerminalDelivery(
                item["delivery_id"],
                WorkflowRunId(item["run_id"]),
                item["destination_id"],
                item["outcome_payload"],
                ReconcileState(item["state"]),
                item["revision"],
                item["attempts"],
                AbsoluteInstant.from_dict(item["next_eligible_at"]),
                item["reason"],
            )
            deliveries[record.delivery_id] = record
        for item in raw["governance_cancellations"]:
            record = _decode_governance(item)
            if record.request.request_id in governance_cancellations:
                raise ValueError("duplicate Workflow governance cancellation identity")
            governance_cancellations[record.request.request_id] = record
        return {
            "effects": effects,
            "deliveries": deliveries,
            "governance_cancellations": governance_cancellations,
        }

    def _write(self, state) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": _SCHEMA,
            "effects": [
                _effect_dict(item) for item in sorted(state["effects"].values(), key=lambda value: value.effect_id)
            ],
            "deliveries": [
                _delivery_dict(item)
                for item in sorted(state["deliveries"].values(), key=lambda value: value.delivery_id)
            ],
            "governance_cancellations": [
                _governance_dict(item)
                for item in sorted(
                    state["governance_cancellations"].values(),
                    key=lambda value: value.request.request_id,
                )
            ],
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


class WorkflowGovernanceCancellationInbox:
    """Verified public command/query surface over the canonical reconciliation store."""

    def __init__(
        self,
        store: WorkflowReconciliationStore,
        verifier: WorkflowGovernanceSnapshotVerifierPort,
    ) -> None:
        self._store = store
        self._verifier = verifier

    def submit(self, request: WorkflowGovernanceCancelRequest) -> WorkflowGovernanceCancelAcceptance:
        verification = self._verifier.verify(request)
        rejected = {
            WorkflowGovernanceSnapshotVerification.SCOPE_MISMATCH: WorkflowGovernanceAcceptanceDisposition.SCOPE_MISMATCH,
            WorkflowGovernanceSnapshotVerification.STALE_EPOCH: WorkflowGovernanceAcceptanceDisposition.STALE_EPOCH,
            WorkflowGovernanceSnapshotVerification.FENCE_LOST: WorkflowGovernanceAcceptanceDisposition.FENCE_LOST,
        }
        if verification is not WorkflowGovernanceSnapshotVerification.VERIFIED:
            return WorkflowGovernanceCancelAcceptance(
                request.request_id,
                rejected[verification],
                None,
                len(request.target_agent_ids),
            )
        return self._store.submit_verified(request)

    def get(
        self, request_id: WorkflowGovernanceScopeCancelRequestId
    ) -> WorkflowGovernanceCancelSettlementSnapshot | None:
        return self._store.get(request_id)


class WorkflowGovernanceCancellationReconciler:
    """Fenced durable delivery from frozen lineage scope to WorkflowRunControl."""

    def __init__(
        self,
        store: WorkflowReconciliationStore,
        ownership,
        runs: WorkflowRunStore,
        control: WorkflowRunControl,
        admissions: WorkflowGovernanceAdmissionQueryPort,
        *,
        deployment_id: str,
        holder_id: str,
        backend: OperationBackend,
        now: Callable[[], AbsoluteInstant],
        max_attempts: int = 3,
    ) -> None:
        self._store = store
        self._ownership = ownership
        self._runs = runs
        self._control = control
        self._admissions = admissions
        self._deployment_id = deployment_id
        self._holder_id = holder_id
        self._backend = backend
        self._now = now
        self._max_attempts = max_attempts

    def reconcile(self) -> int:
        progressed = 0
        now = self._now().epoch_nanoseconds
        for observed in self._store.records()["governance_cancellations"].values():
            if (
                observed.lifecycle
                not in {
                    WorkflowGovernanceSettlementLifecycle.PENDING,
                    WorkflowGovernanceSettlementLifecycle.PARTIAL,
                }
                or observed.next_eligible_at.epoch_nanoseconds > now
            ):
                continue
            try:
                ownership = self._claim(observed)
            except (LeaseUnavailableError, LeaseCoordinatorUnavailableError):
                continue
            try:
                current = self._store.records()["governance_cancellations"].get(observed.request.request_id)
                if current is None or current != observed:
                    continue
                self._reconcile_claimed(current, ownership)
                progressed += 1
            finally:
                self._ownership.release(ownership)
        return progressed

    def _reconcile_claimed(self, item: WorkflowGovernanceCancellation, ownership) -> None:
        admissions = tuple(
            self._admissions.get_workflow_create_admission(admission_id)
            for admission_id in item.request.admitted_workflow_create_ids
        )
        unresolved = any(
            admission is None or admission.lifecycle is WorkflowCreateAdmissionLifecycle.RESERVED
            for admission in admissions
        )
        committed_ids = {
            admission.admission_id
            for admission in admissions
            if admission is not None and admission.lifecycle is WorkflowCreateAdmissionLifecycle.COMMITTED
        }
        joined = tuple(
            run
            for run in self._runs.scan()
            if run.provenance.workflow_create_admission_id in committed_ids
            and run.provenance.creator_logical_agent_id in item.request.target_agent_ids
            and run.access_grant.root_governance_agent_id == item.request.root_agent_id
        )
        if len(joined) != len(committed_ids):
            unresolved = True
        settlements = {row.reference: row for row in item.per_run_settlements}
        for run in joined:
            if run.phase.terminal:
                disposition = WorkflowGovernanceRunDisposition.ALREADY_TERMINAL
                revision = run.revision
            elif run.phase is WorkflowRunPhase.CANCELLING:
                disposition = WorkflowGovernanceRunDisposition.ALREADY_CANCELLING
                revision = run.revision
            else:
                try:
                    cancelled = self._control.cancel(WorkflowRunCommand(run.reference, run.revision))
                except (KeyError, RuntimeError):
                    unresolved = True
                    disposition = WorkflowGovernanceRunDisposition.RETRY_PENDING
                    revision = run.revision
                else:
                    disposition = WorkflowGovernanceRunDisposition.CANCEL_INTENT_APPLIED
                    revision = cancelled.revision
            settlements[run.reference] = WorkflowGovernanceRunSettlement(
                run.reference,
                self.per_run_request_id(item.request.request_id, run.reference.run_id),
                revision,
                disposition,
            )
        attempts = item.attempts + 1
        if unresolved and attempts >= self._max_attempts:
            lifecycle = WorkflowGovernanceSettlementLifecycle.DEAD_LETTER
            settlements = {
                reference: (
                    row
                    if row.disposition is not WorkflowGovernanceRunDisposition.RETRY_PENDING
                    else WorkflowGovernanceRunSettlement(
                        row.reference,
                        row.per_run_request_id,
                        row.revision,
                        WorkflowGovernanceRunDisposition.DEAD_LETTER,
                    )
                )
                for reference, row in settlements.items()
            }
        elif unresolved:
            lifecycle = WorkflowGovernanceSettlementLifecycle.PARTIAL
        else:
            lifecycle = WorkflowGovernanceSettlementLifecycle.SETTLED
        updated = WorkflowGovernanceCancellation(
            item.request,
            item.revision + 1,
            lifecycle,
            tuple(sorted(settlements.values(), key=lambda row: row.reference.run_id)),
            attempts,
            self._retry_at(attempts) if unresolved else item.next_eligible_at,
            "unresolved frozen admission or run" if unresolved else "",
        )
        self._store.commit_governance(updated, item.revision, ownership)

    @staticmethod
    def per_run_request_id(request_id: WorkflowGovernanceScopeCancelRequestId, run_id: WorkflowRunId) -> str:
        return "wgcr_" + hashlib.sha256(f"{request_id}\0{run_id}".encode()).hexdigest()

    def _claim(self, item: WorkflowGovernanceCancellation):
        identity = str(item.request.request_id)
        return self._ownership.claim(
            OperationOwnershipRequest(
                self._deployment_id,
                identity,
                self._holder_id,
                self._backend,
                item.revision,
                identity,
                EffectCapability.IDEMPOTENT_BY_KEY,
            ),
            30.0,
        )

    def _retry_at(self, attempts: int) -> AbsoluteInstant:
        now = self._now()
        return AbsoluteInstant(
            1,
            now.clock,
            now.epoch_nanoseconds + min(2 ** max(0, attempts - 1), 60) * 1_000_000_000,
        )


class WorkflowReconciler:
    def __init__(
        self,
        store,
        ownership,
        *,
        deployment_id: str,
        holder_id: str,
        backend: OperationBackend,
        now: Callable[[], AbsoluteInstant],
        max_attempts: int = 3,
    ) -> None:
        self._store = store
        self._ownership = ownership
        self._deployment_id = deployment_id
        self._holder_id = holder_id
        self._backend = backend
        self._now = now
        self._max_attempts = max_attempts

    def submit_effect(
        self, run_id: WorkflowRunId, logical_key: str, capability: EffectCapability, payload: str
    ) -> WorkflowEffect:
        effect_id = self._store.effect_identity(run_id, logical_key)
        ownership = self._claim(effect_id, capability, 0)
        try:
            current = self._store.records()["effects"].get(effect_id)
            if current is not None:
                return current
            item = WorkflowEffect(
                effect_id, run_id, capability, payload, "", ReconcileState.AVAILABLE, 1, 0, self._now()
            )
            return self._store.commit_effect(item, None, ownership)
        finally:
            self._ownership.release(ownership)

    def submit_terminal(self, run_id: WorkflowRunId, destination_id: str, payload: str) -> WorkflowTerminalDelivery:
        delivery_id = self._store.delivery_identity(run_id, destination_id)
        ownership = self._claim(delivery_id, EffectCapability.IDEMPOTENT_BY_KEY, 0)
        try:
            current = self._store.records()["deliveries"].get(delivery_id)
            if current is not None:
                return current
            item = WorkflowTerminalDelivery(
                delivery_id, run_id, destination_id, payload, ReconcileState.AVAILABLE, 1, 0, self._now()
            )
            return self._store.commit_delivery(item, None, ownership)
        finally:
            self._ownership.release(ownership)

    def pending_terminal_destinations(self, prefix: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                delivery.destination_id
                for delivery in self._store.records()["deliveries"].values()
                if delivery.destination_id.startswith(prefix)
                and delivery.state not in {ReconcileState.SETTLED, ReconcileState.DEAD_LETTER}
            )
        )

    def reconcile_effects(
        self,
        execute: Callable[[WorkflowEffect], tuple[EffectSettlement, str]],
        *,
        eligible: Callable[[WorkflowEffect], bool] | None = None,
    ) -> int:
        progressed = 0
        for item in self._eligible("effects", eligible):
            ownership = self._claim(item.effect_id, item.capability, item.revision)
            try:
                claimed = self._replace_effect(
                    item, ownership, state=ReconcileState.CLAIMED, attempts=item.attempts + 1
                )
                try:
                    settlement, receipt = execute(claimed)
                except Exception as exc:
                    self._effect_failure(claimed, ownership, str(exc))
                else:
                    state = (
                        ReconcileState.SETTLED
                        if settlement in {EffectSettlement.SUCCEEDED, EffectSettlement.FAILED}
                        else ReconcileState.IN_DOUBT
                    )
                    self._replace_effect(claimed, ownership, state=state, provider_receipt=receipt)
                progressed += 1
            finally:
                self._ownership.release(ownership)
        return progressed

    async def reconcile_effects_async(
        self,
        execute: Callable[[WorkflowEffect], Awaitable[tuple[EffectSettlement, str]]],
        *,
        eligible: Callable[[WorkflowEffect], bool] | None = None,
    ) -> int:
        progressed = 0
        for item in self._eligible("effects", eligible):
            ownership = self._claim(item.effect_id, item.capability, item.revision)
            try:
                claimed = self._replace_effect(
                    item,
                    ownership,
                    state=ReconcileState.CLAIMED,
                    attempts=item.attempts + 1,
                )
                try:
                    settlement, receipt = await execute(claimed)
                except Exception as exc:
                    self._effect_failure(claimed, ownership, str(exc))
                else:
                    state = (
                        ReconcileState.SETTLED
                        if settlement in {EffectSettlement.SUCCEEDED, EffectSettlement.FAILED}
                        else ReconcileState.IN_DOUBT
                    )
                    self._replace_effect(
                        claimed,
                        ownership,
                        state=state,
                        provider_receipt=receipt,
                    )
                progressed += 1
            finally:
                self._ownership.release(ownership)
        return progressed

    def reconcile_deliveries(
        self,
        deliver: Callable[[WorkflowTerminalDelivery], bool],
        *,
        eligible: Callable[[WorkflowTerminalDelivery], bool] | None = None,
    ) -> int:
        progressed = 0
        for item in self._eligible("deliveries", eligible):
            ownership = self._claim(item.delivery_id, EffectCapability.IDEMPOTENT_BY_KEY, item.revision)
            try:
                claimed = self._replace_delivery(
                    item, ownership, state=ReconcileState.CLAIMED, attempts=item.attempts + 1
                )
                try:
                    acknowledged = deliver(claimed)
                except Exception as exc:
                    self._delivery_failure(claimed, ownership, str(exc))
                else:
                    if acknowledged:
                        self._replace_delivery(claimed, ownership, state=ReconcileState.SETTLED)
                    else:
                        self._delivery_failure(claimed, ownership, "not acknowledged")
                progressed += 1
            finally:
                self._ownership.release(ownership)
        return progressed

    def _eligible(self, collection, predicate=None):
        now = self._now().epoch_nanoseconds
        return tuple(
            item
            for item in self._store.records()[collection].values()
            if item.state is ReconcileState.AVAILABLE
            and item.next_eligible_at.epoch_nanoseconds <= now
            and (predicate is None or predicate(item))
        )

    def _effect_failure(self, item, ownership, reason):
        guarantee = project_operation_guarantee(self._backend, item.capability)
        if not guarantee.automatic_retry_allowed:
            self._replace_effect(item, ownership, state=ReconcileState.IN_DOUBT, reason=reason)
        elif item.attempts >= self._max_attempts:
            self._replace_effect(item, ownership, state=ReconcileState.DEAD_LETTER, reason=reason)
        else:
            self._replace_effect(
                item,
                ownership,
                state=ReconcileState.AVAILABLE,
                reason=reason,
                next_eligible_at=self._retry_at(item.attempts),
            )

    def _delivery_failure(self, item, ownership, reason):
        state = ReconcileState.DEAD_LETTER if item.attempts >= self._max_attempts else ReconcileState.AVAILABLE
        self._replace_delivery(
            item,
            ownership,
            state=state,
            reason=reason,
            next_eligible_at=self._retry_at(item.attempts),
        )

    def _replace_effect(self, item, ownership, **changes):
        updated = WorkflowEffect(
            item.effect_id,
            item.run_id,
            item.capability,
            item.command_payload,
            changes.get("provider_receipt", item.provider_receipt),
            changes.get("state", item.state),
            item.revision + 1,
            changes.get("attempts", item.attempts),
            changes.get("next_eligible_at", item.next_eligible_at),
            changes.get("reason", item.reason),
        )
        return self._store.commit_effect(updated, item.revision, ownership)

    def _replace_delivery(self, item, ownership, **changes):
        updated = WorkflowTerminalDelivery(
            item.delivery_id,
            item.run_id,
            item.destination_id,
            item.outcome_payload,
            changes.get("state", item.state),
            item.revision + 1,
            changes.get("attempts", item.attempts),
            changes.get("next_eligible_at", item.next_eligible_at),
            changes.get("reason", item.reason),
        )
        return self._store.commit_delivery(updated, item.revision, ownership)

    def _claim(self, identity, capability, revision):
        return self._ownership.claim(
            OperationOwnershipRequest(
                self._deployment_id,
                identity,
                self._holder_id,
                self._backend,
                revision,
                identity,
                capability,
            ),
            30.0,
        )

    def _retry_at(self, attempts: int) -> AbsoluteInstant:
        now = self._now()
        delay_seconds = min(2 ** max(0, attempts - 1), 60)
        return AbsoluteInstant(1, now.clock, now.epoch_nanoseconds + delay_seconds * 1_000_000_000)


def _strict_item(item, fields):
    if type(item) is not dict or set(item) != fields:
        raise ValueError("workflow reconciliation record shape is invalid")
    for key in fields - {"revision", "attempts", "next_eligible_at"}:
        if type(item[key]) is not str:
            raise ValueError("workflow reconciliation string field is invalid")
    if (
        type(item["revision"]) is not int
        or item["revision"] < 1
        or type(item["attempts"]) is not int
        or item["attempts"] < 0
    ):
        raise ValueError("workflow reconciliation counter is invalid")


def _effect_dict(item):
    return {
        "effect_id": item.effect_id,
        "run_id": str(item.run_id),
        "capability": item.capability.value,
        "command_payload": item.command_payload,
        "provider_receipt": item.provider_receipt,
        "state": item.state.value,
        "revision": item.revision,
        "attempts": item.attempts,
        "next_eligible_at": item.next_eligible_at.to_dict(),
        "reason": item.reason,
    }


def _delivery_dict(item):
    return {
        "delivery_id": item.delivery_id,
        "run_id": str(item.run_id),
        "destination_id": item.destination_id,
        "outcome_payload": item.outcome_payload,
        "state": item.state.value,
        "revision": item.revision,
        "attempts": item.attempts,
        "next_eligible_at": item.next_eligible_at.to_dict(),
        "reason": item.reason,
    }


def _governance_dict(item: WorkflowGovernanceCancellation) -> dict[str, object]:
    return {
        "request": encode_workflow_governance_cancel(item.request),
        "revision": item.revision,
        "lifecycle": item.lifecycle.value,
        "per_run_settlements": [
            {
                "run_id": str(settlement.reference.run_id),
                "definition_id": str(settlement.reference.definition_id),
                "per_run_request_id": settlement.per_run_request_id,
                "revision": settlement.revision,
                "disposition": settlement.disposition.value,
            }
            for settlement in item.per_run_settlements
        ],
        "attempts": item.attempts,
        "next_eligible_at": item.next_eligible_at.to_dict(),
        "reason": item.reason,
    }


def _decode_governance(raw: object) -> WorkflowGovernanceCancellation:
    fields = {
        "request",
        "revision",
        "lifecycle",
        "per_run_settlements",
        "attempts",
        "next_eligible_at",
        "reason",
    }
    if type(raw) is not dict or set(raw) != fields:
        raise ValueError("Workflow governance reconciliation record shape is invalid")
    if (
        type(raw["revision"]) is not int
        or raw["revision"] < 1
        or type(raw["attempts"]) is not int
        or raw["attempts"] < 0
        or type(raw["reason"]) is not str
        or type(raw["lifecycle"]) is not str
        or type(raw["per_run_settlements"]) is not list
    ):
        raise ValueError("Workflow governance reconciliation primitive is invalid")
    settlements: list[WorkflowGovernanceRunSettlement] = []
    settlement_fields = {"run_id", "definition_id", "per_run_request_id", "revision", "disposition"}
    for row in raw["per_run_settlements"]:
        if type(row) is not dict or set(row) != settlement_fields:
            raise ValueError("Workflow governance per-run settlement shape is invalid")
        if (
            type(row["run_id"]) is not str
            or type(row["definition_id"]) is not str
            or type(row["per_run_request_id"]) is not str
            or not row["per_run_request_id"]
            or type(row["revision"]) is not int
            or row["revision"] < 1
            or type(row["disposition"]) is not str
        ):
            raise ValueError("Workflow governance per-run settlement primitive is invalid")
        settlements.append(
            WorkflowGovernanceRunSettlement(
                WorkflowRunReference(
                    WorkflowRunId(row["run_id"]),
                    WorkflowDefinitionId(row["definition_id"]),
                ),
                row["per_run_request_id"],
                row["revision"],
                WorkflowGovernanceRunDisposition(row["disposition"]),
            )
        )
    if len({item.reference for item in settlements}) != len(settlements):
        raise ValueError("Workflow governance per-run settlement is duplicated")
    return WorkflowGovernanceCancellation(
        decode_workflow_governance_cancel(raw["request"]),
        raw["revision"],
        WorkflowGovernanceSettlementLifecycle(raw["lifecycle"]),
        tuple(settlements),
        raw["attempts"],
        AbsoluteInstant.from_dict(raw["next_eligible_at"]),
        raw["reason"],
    )


__all__ = [
    "ReconcileState",
    "WorkflowEffect",
    "WorkflowGovernanceCancellation",
    "WorkflowGovernanceCancellationInbox",
    "WorkflowGovernanceCancellationReconciler",
    "WorkflowReconciler",
    "WorkflowReconciliationStore",
    "WorkflowTerminalDelivery",
]
