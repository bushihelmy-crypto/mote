"""Crash-safe local journal for externally hosted Tool service calls."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import threading
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from mote.contracts.service import (
    PendingServiceCall,
    ServiceAttemptFinishedRecord,
    ServiceAttemptStartedRecord,
    ServiceCallFinishedRecord,
    ServiceCallJournalRecord,
    ServiceCallOwnerEpoch,
    ServiceCallPlannedRecord,
    ServiceCallRecovery,
    ServiceCallState,
    ServiceCallSuspendedRecord,
    ServiceCancelCommand,
    ServiceCancelReceipt,
    ServiceDecisionRecord,
    ServiceInvocation,
    ServiceReceiptAcceptedRecord,
)
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.persistence.atomic import atomic_write
from mote.runtime.telemetry.logging import log_class

SERVICE_CALL_JOURNAL_DIRNAME = "service-calls"
SERVICE_CALL_ROOT_MANIFEST_SCHEMA = "mote.service-call-root/v3"
_RECORD_ADAPTER = TypeAdapter(ServiceCallJournalRecord)


class ServiceCallJournalError(RuntimeError):
    pass


class ServiceCallJournalIntegrityError(ServiceCallJournalError):
    pass


class ServiceCallJournalUnavailableError(ServiceCallJournalError):
    pass


class ServiceCallOwnershipLostError(ServiceCallJournalError):
    pass


class _OwnershipClaim:
    def __init__(self, journal: "LocalServiceCallJournal", service_call_id: str) -> None:
        self._journal = journal
        self.service_call_id = service_call_id
        self.generation = 0
        self.fencing_token = 0
        self.revision = 0
        self.tail_revision = 0
        self._descriptor: int | None = None
        self._active = False
        self._token: Token[_OwnershipClaim | None] | None = None

    async def __aenter__(self) -> "_OwnershipClaim":
        await run_disk_io(self._acquire)
        self._token = self._journal._claim_context.set(self)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._token is not None:
            self._journal._claim_context.reset(self._token)
            self._token = None
        await run_disk_io(self._release)

    def _acquire(self) -> None:
        lock_path = self._journal.lock_path_for(self.service_call_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            records = self._journal._read_path(
                self._journal.path_for(self.service_call_id),
                expected_call_id=self.service_call_id,
            )
            prior = self._journal._read_owner_epoch(self.service_call_id)
            generation = 1 if prior is None else prior.generation + 1
            revision = 1 if prior is None else prior.revision + 1
            epoch = ServiceCallOwnerEpoch(
                service_call_id=self.service_call_id,
                owner_id=self._journal._owner_id,
                generation=generation,
                fencing_token=generation,
                revision=revision,
            )
            self._journal._write_owner_epoch(epoch)
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self.generation = generation
        self.fencing_token = generation
        self.revision = revision
        self.tail_revision = len(records)
        self._active = True

    def _release(self) -> None:
        descriptor = self._descriptor
        self._active = False
        self._descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def require_active(self, service_call_id: str) -> None:
        if not self._active or self.service_call_id != service_call_id:
            raise ServiceCallOwnershipLostError("service-call ownership claim is stale")


@log_class(level="DEBUG", exclude={"path_for", "records"})
class LocalServiceCallJournal:
    """One append-only, fsynced JSONL stream per logical service call."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._owner_id = f"service-call-journal:{uuid4().hex}"
        self._lock = threading.Lock()
        self._claim_context: ContextVar[_OwnershipClaim | None] = ContextVar(
            f"service-call-claim:{id(self)}", default=None
        )
        self._activation_checked = False

    def claim(self, service_call_id: str) -> _OwnershipClaim:
        self._ensure_activation()
        self.path_for(service_call_id)
        return _OwnershipClaim(self, service_call_id)

    def path_for(self, service_call_id: str) -> Path:
        if type(service_call_id) is not str or not service_call_id:
            raise ValueError("service_call_id must be a non-empty string")
        digest = hashlib.sha256(service_call_id.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.jsonl"

    def lock_path_for(self, service_call_id: str) -> Path:
        return self.path_for(service_call_id).with_suffix(".lock")

    def generation_path_for(self, service_call_id: str) -> Path:
        return self.path_for(service_call_id).with_suffix(".owner.json")

    def cancellation_path_for(self, service_call_id: str) -> Path:
        return self.path_for(service_call_id).with_suffix(".cancel")

    async def request_cancel(self, command: ServiceCancelCommand) -> ServiceCancelReceipt:
        return await run_disk_io(self._request_cancel_committed, command)

    def _request_cancel_committed(self, command: ServiceCancelCommand) -> ServiceCancelReceipt:
        records = self.records(command.service_call_id)
        if len(records) != command.expected_stream_revision:
            raise ServiceCallOwnershipLostError("service-call cancel expectation is stale")
        path = self.cancellation_path_for(command.service_call_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = self._read_cancel(command.service_call_id)
            if existing.command_id != command.command_id or existing != command:
                raise ServiceCallJournalIntegrityError("service-call cancel command conflicts")
            return ServiceCancelReceipt(
                command_id=command.command_id,
                service_call_id=command.service_call_id,
                command_revision=1,
            )
        payload = command.model_dump_json().encode()
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = self._read_cancel(command.service_call_id)
            if existing != command:
                raise ServiceCallJournalIntegrityError("service-call cancel command conflicts")
            return ServiceCancelReceipt(
                command_id=command.command_id,
                service_call_id=command.service_call_id,
                command_revision=1,
            )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return ServiceCancelReceipt(
            command_id=command.command_id,
            service_call_id=command.service_call_id,
            command_revision=1,
        )

    def cancellation_requested(self, service_call_id: str) -> bool:
        self._ensure_activation()
        path = self.cancellation_path_for(service_call_id)
        if not path.is_file():
            return False
        self._read_cancel(service_call_id)
        return True

    async def append(self, record: ServiceCallJournalRecord) -> None:
        claim = self._claim_context.get()
        if claim is None:
            raise ServiceCallOwnershipLostError("service-call append requires an ownership claim")
        task = asyncio.create_task(run_disk_io(self.append_committed, record, claim))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    def append_committed(self, record: ServiceCallJournalRecord, claim: _OwnershipClaim) -> None:
        path = self.path_for(record.service_call_id)
        payload = record.model_dump_json().encode("utf-8") + b"\n"
        claim.require_active(record.service_call_id)
        with self._lock:
            existing = self._read_path(path, expected_call_id=record.service_call_id)
            if len(existing) != claim.tail_revision:
                raise ServiceCallOwnershipLostError("service-call journal tail revision changed")
            self._validate_append(existing, record)
            try:
                existed = path.exists()
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(
                    path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                try:
                    written = 0
                    while written < len(payload):
                        count = os.write(descriptor, payload[written:])
                        if count <= 0:
                            raise OSError("service-call journal append made no progress")
                        written += count
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if not existed:
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                claim.tail_revision += 1
            except OSError as exc:
                raise ServiceCallJournalUnavailableError("service-call journal cannot be appended") from exc

    def _read_owner_epoch(self, service_call_id: str) -> ServiceCallOwnerEpoch | None:
        path = self.generation_path_for(service_call_id)
        if not path.exists():
            return None
        try:
            epoch = ServiceCallOwnerEpoch.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ServiceCallJournalIntegrityError("service-call owner generation is invalid") from exc
        if epoch.service_call_id != service_call_id:
            raise ServiceCallJournalIntegrityError("service-call owner identity is inconsistent")
        return epoch

    def _write_owner_epoch(self, epoch: ServiceCallOwnerEpoch) -> None:
        path = self.generation_path_for(epoch.service_call_id)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        payload = epoch.model_dump_json().encode()
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _read_cancel(self, service_call_id: str) -> ServiceCancelCommand:
        try:
            command = ServiceCancelCommand.model_validate_json(self.cancellation_path_for(service_call_id).read_bytes())
        except (OSError, ValueError) as exc:
            raise ServiceCallJournalIntegrityError("service-call cancel command is invalid") from exc
        if command.service_call_id != service_call_id:
            raise ServiceCallJournalIntegrityError("service-call cancel identity is inconsistent")
        return command

    def records(self, service_call_id: str) -> tuple[ServiceCallJournalRecord, ...]:
        self._ensure_activation()
        path = self.path_for(service_call_id)
        with self._lock:
            return self._read_path(path, expected_call_id=service_call_id)

    def recover(self, service_call_id: str) -> ServiceCallRecovery:
        return self._recover_records(self.records(service_call_id))

    def scan_terminal_before(self, *, before: datetime, after: str | None = None, limit: int = 128) -> tuple[str, ...]:
        """Return a bounded, deterministic page of terminal calls eligible for retention."""
        if before.tzinfo is None or limit < 1 or limit > 4096:
            raise ValueError("service-call retention scan bounds are invalid")
        self._ensure_activation()
        result: list[str] = []
        for path in sorted(self._root.glob("*.jsonl"), key=lambda item: item.name):
            if after is not None and path.name <= after:
                continue
            records = self._read_path(path, expected_call_id=None)
            recovery = self._recover_records(records)
            terminal = recovery.terminal
            if terminal is None or terminal.occurred_at.astimezone(timezone.utc) > before.astimezone(timezone.utc):
                continue
            result.append(path.stem)
            if len(result) >= limit:
                break
        return tuple(result)

    async def pending_calls(self, *, after: str | None, limit: int) -> tuple[PendingServiceCall, ...]:
        if limit < 1 or limit > 256:
            raise ValueError("pending service-call page limit must be between 1 and 256")
        return await run_disk_io(self._pending_calls_committed, after, limit)

    def _pending_calls_committed(self, after: str | None, limit: int) -> tuple[PendingServiceCall, ...]:
        self._ensure_activation()
        if not self._root.exists():
            return ()
        result: list[PendingServiceCall] = []
        for path in sorted(self._root.glob("*.jsonl"), key=lambda item: item.name):
            if after is not None and path.name <= after:
                continue
            records = self._read_path(path, expected_call_id=None)
            if not records:
                continue
            recovery = self._recover_records(records)
            if recovery.terminal is not None or not recovery.receipts:
                continue
            plan = recovery.plans[0]
            result.append(
                PendingServiceCall(
                    invocation=ServiceInvocation(
                        service_call_id=plan.service_call_id,
                        route_id=plan.route_id,
                        capability=plan.capability,
                        payload=plan.payload,
                        semantics=plan.semantics,
                        idempotency_key=plan.idempotency_key,
                    ),
                    stream_revision=len(records),
                    cursor=path.name,
                )
            )
            if len(result) == limit:
                break
        return tuple(result)

    def _ensure_activation(self) -> None:
        if self._activation_checked:
            return
        manifest = self._root / "activation-manifest.json"
        if manifest.exists():
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ServiceCallJournalIntegrityError("service-call activation manifest is unreadable") from exc
            if (
                type(raw) is not dict
                or set(raw) != {"schema", "generation", "source_digest", "evidence_retention_days"}
                or raw["schema"] != SERVICE_CALL_ROOT_MANIFEST_SCHEMA
                or type(raw["generation"]) is not int
                or raw["generation"] < 1
                or type(raw["source_digest"]) is not str
                or raw["evidence_retention_days"] != 180
            ):
                raise ServiceCallJournalIntegrityError("service-call activation manifest is invalid")
            self._activation_checked = True
            return
        durable = (
            tuple(self._root.glob("*.jsonl"))
            + tuple(self._root.glob("*.owner.json"))
            + tuple(self._root.glob("*.cancel"))
        )
        if durable:
            raise ServiceCallJournalIntegrityError("service-call root requires explicit v3 migration")
        atomic_write(
            manifest,
            json.dumps(
                {
                    "schema": SERVICE_CALL_ROOT_MANIFEST_SCHEMA,
                    "generation": 1,
                    "source_digest": "empty",
                    "evidence_retention_days": 180,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            mode=0o600,
        )
        self._activation_checked = True

    @staticmethod
    def _read_path(
        path: Path,
        *,
        expected_call_id: str | None,
    ) -> tuple[ServiceCallJournalRecord, ...]:
        if not path.exists():
            return ()
        records: list[ServiceCallJournalRecord] = []
        try:
            with path.open("rb") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.endswith(b"\n"):
                        raise ServiceCallJournalIntegrityError(f"service-call journal line {line_number} is incomplete")
                    try:
                        record = _RECORD_ADAPTER.validate_json(line)
                    except ValidationError as exc:
                        raise ServiceCallJournalIntegrityError(
                            f"service-call journal line {line_number} is invalid"
                        ) from exc
                    if expected_call_id is not None and record.service_call_id != expected_call_id:
                        raise ServiceCallJournalIntegrityError(
                            "service-call journal file identity does not match its record"
                        )
                    records.append(record)
        except OSError as exc:
            raise ServiceCallJournalIntegrityError("service-call journal cannot be read") from exc
        LocalServiceCallJournal._validate_stream(tuple(records))
        return tuple(records)

    @staticmethod
    def _validate_append(
        existing: tuple[ServiceCallJournalRecord, ...],
        record: ServiceCallJournalRecord,
    ) -> None:
        if not existing:
            if not isinstance(record, ServiceCallPlannedRecord):
                raise ServiceCallJournalIntegrityError("service-call journal must begin with service_call_planned")
            return
        if isinstance(existing[-1], ServiceCallFinishedRecord):
            raise ServiceCallJournalIntegrityError("service call is already terminal")
        LocalServiceCallJournal._validate_stream(existing + (record,))

    @staticmethod
    def _validate_stream(records: tuple[ServiceCallJournalRecord, ...]) -> None:
        if not records:
            return
        first = records[0]
        if not isinstance(first, ServiceCallPlannedRecord):
            raise ServiceCallJournalIntegrityError("service-call journal must begin with service_call_planned")
        if first.resume_generation != 0:
            raise ServiceCallJournalIntegrityError("service-call journal first plan must be generation zero")
        call_id = first.service_call_id
        plan = first
        starts: dict[str, ServiceAttemptStartedRecord] = {}
        finished: set[str] = set()
        receipt_ordinals: dict[str, int] = {}
        terminal = False
        for record in records[1:]:
            if record.service_call_id != call_id:
                raise ServiceCallJournalIntegrityError("service-call journal contains mixed call identities")
            if terminal:
                raise ServiceCallJournalIntegrityError("service-call journal contains records after terminal")
            if isinstance(record, ServiceCallPlannedRecord):
                if set(starts) != finished:
                    raise ServiceCallJournalIntegrityError("resume generation began with an open attempt")
                if record.resume_generation != plan.resume_generation + 1:
                    raise ServiceCallJournalIntegrityError("resume generation is not contiguous")
                plan = record
            elif isinstance(record, ServiceAttemptStartedRecord):
                if record.attempt_id in starts or record.ordinal != len(starts) + 1:
                    raise ServiceCallJournalIntegrityError("service attempt identity or ordinal is inconsistent")
                if record.resume_generation != plan.resume_generation or record.endpoint_id not in plan.endpoint_ids:
                    raise ServiceCallJournalIntegrityError("service attempt does not belong to the active generation")
                starts[record.attempt_id] = record
            elif isinstance(record, ServiceReceiptAcceptedRecord):
                if record.attempt_id not in starts or record.attempt_id in finished:
                    raise ServiceCallJournalIntegrityError("service receipt does not belong to an open attempt")
                expected = receipt_ordinals.get(record.attempt_id, -1) + 1
                if record.poll_ordinal != expected:
                    raise ServiceCallJournalIntegrityError("service receipt poll ordinal is not contiguous")
                receipt_ordinals[record.attempt_id] = record.poll_ordinal
            elif isinstance(record, ServiceCallSuspendedRecord):
                if (
                    record.attempt_id not in starts
                    or record.attempt_id in finished
                    or record.attempt_id not in receipt_ordinals
                    or record.resume_generation != plan.resume_generation
                ):
                    raise ServiceCallJournalIntegrityError(
                        "service suspension does not belong to an accepted open attempt"
                    )
            elif isinstance(record, ServiceAttemptFinishedRecord):
                start = starts.get(record.attempt_id)
                if (
                    start is None
                    or record.attempt_id in finished
                    or record.ordinal != start.ordinal
                    or record.resume_generation != start.resume_generation
                ):
                    raise ServiceCallJournalIntegrityError("service attempt terminal does not match one open attempt")
                finished.add(record.attempt_id)
            elif isinstance(record, ServiceDecisionRecord):
                if record.resume_generation != plan.resume_generation or record.after_attempt_ordinal > len(starts):
                    raise ServiceCallJournalIntegrityError("service decision does not belong to the active generation")
            elif isinstance(record, ServiceCallFinishedRecord):
                if set(starts) != finished:
                    raise ServiceCallJournalIntegrityError("service call became terminal with an open attempt")
                terminal = True

    @staticmethod
    def _recover_records(
        records: tuple[ServiceCallJournalRecord, ...],
    ) -> ServiceCallRecovery:
        if not records or not isinstance(records[0], ServiceCallPlannedRecord):
            raise ServiceCallJournalIntegrityError("service call has no plan record")
        plans = tuple(record for record in records if isinstance(record, ServiceCallPlannedRecord))
        starts = tuple(record for record in records if isinstance(record, ServiceAttemptStartedRecord))
        finishes = tuple(record for record in records if isinstance(record, ServiceAttemptFinishedRecord))
        receipts = tuple(record for record in records if isinstance(record, ServiceReceiptAcceptedRecord))
        finished_ids = {record.attempt_id for record in finishes}
        open_attempt = next(
            (record for record in reversed(starts) if record.attempt_id not in finished_ids),
            None,
        )
        terminal = next(
            (record for record in reversed(records) if isinstance(record, ServiceCallFinishedRecord)),
            None,
        )
        if terminal is not None:
            state = terminal.state
        elif open_attempt is not None and not any(
            receipt.attempt_id == open_attempt.attempt_id for receipt in receipts
        ):
            state = ServiceCallState.IN_DOUBT
        elif any(isinstance(record, ServiceCallSuspendedRecord) for record in records):
            state = ServiceCallState.WAITING_REMOTE
        elif starts:
            state = ServiceCallState.RUNNING
        else:
            state = ServiceCallState.PLANNED
        return ServiceCallRecovery(
            service_call_id=plans[-1].service_call_id,
            state=state,
            plan=plans[-1],
            plans=plans,
            attempt_starts=starts,
            receipts=receipts,
            suspensions=tuple(record for record in records if isinstance(record, ServiceCallSuspendedRecord)),
            attempt_finishes=finishes,
            decisions=tuple(record for record in records if isinstance(record, ServiceDecisionRecord)),
            terminal=terminal,
        )


def service_call_journal_root(workspace_root: Path) -> Path:
    return workspace_root / ".runtime" / SERVICE_CALL_JOURNAL_DIRNAME


__all__ = [
    "LocalServiceCallJournal",
    "SERVICE_CALL_ROOT_MANIFEST_SCHEMA",
    "SERVICE_CALL_JOURNAL_DIRNAME",
    "ServiceCallJournalError",
    "ServiceCallJournalIntegrityError",
    "ServiceCallJournalUnavailableError",
    "ServiceCallOwnershipLostError",
    "service_call_journal_root",
]
