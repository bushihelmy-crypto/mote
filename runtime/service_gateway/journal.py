"""Crash-safe local journal for externally hosted Tool service calls."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import threading
from contextvars import ContextVar, Token
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from mote.contracts.service import (
    PendingServiceCall,
    ServiceAttemptFinishedRecord,
    ServiceAttemptStartedRecord,
    ServiceCallFinishedRecord,
    ServiceCallJournalRecord,
    ServiceCallPlannedRecord,
    ServiceCallRecovery,
    ServiceCallState,
    ServiceCallSuspendedRecord,
    ServiceDecisionRecord,
    ServiceInvocation,
    ServiceReceiptAcceptedRecord,
)
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.telemetry.logging import log_class

SERVICE_CALL_JOURNAL_DIRNAME = "service-calls"
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
            generation = self._journal._read_generation(self.service_call_id) + 1
            self._journal._write_generation(self.service_call_id, generation)
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self.generation = generation
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
        self._lock = threading.Lock()
        self._claim_context: ContextVar[_OwnershipClaim | None] = ContextVar(
            f"service-call-claim:{id(self)}", default=None
        )

    def claim(self, service_call_id: str) -> _OwnershipClaim:
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

    async def request_cancel(self, service_call_id: str) -> None:
        await run_disk_io(self._request_cancel_committed, service_call_id)

    def _request_cancel_committed(self, service_call_id: str) -> None:
        path = self.cancellation_path_for(service_call_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return
        try:
            os.write(descriptor, b'{"command":"cancel","schema":1}')
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def cancellation_requested(self, service_call_id: str) -> bool:
        return self.cancellation_path_for(service_call_id).is_file()

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

    def _read_generation(self, service_call_id: str) -> int:
        path = self.generation_path_for(service_call_id)
        if not path.exists():
            return 0
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceCallJournalIntegrityError("service-call owner generation is invalid") from exc
        if type(value) is not dict or set(value) != {"generation"} or type(value["generation"]) is not int:
            raise ServiceCallJournalIntegrityError("service-call owner generation shape is invalid")
        if value["generation"] < 1:
            raise ServiceCallJournalIntegrityError("service-call owner generation must be positive")
        return value["generation"]

    def _write_generation(self, service_call_id: str, generation: int) -> None:
        path = self.generation_path_for(service_call_id)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        payload = json.dumps({"generation": generation}, separators=(",", ":")).encode()
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

    def records(self, service_call_id: str) -> tuple[ServiceCallJournalRecord, ...]:
        path = self.path_for(service_call_id)
        with self._lock:
            return self._read_path(path, expected_call_id=service_call_id)

    def recover(self, service_call_id: str) -> ServiceCallRecovery:
        return self._recover_records(self.records(service_call_id))

    async def pending_calls(self, *, after: str | None, limit: int) -> tuple[PendingServiceCall, ...]:
        if limit < 1 or limit > 256:
            raise ValueError("pending service-call page limit must be between 1 and 256")
        return await run_disk_io(self._pending_calls_committed, after, limit)

    def _pending_calls_committed(self, after: str | None, limit: int) -> tuple[PendingServiceCall, ...]:
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
    "SERVICE_CALL_JOURNAL_DIRNAME",
    "ServiceCallJournalError",
    "ServiceCallJournalIntegrityError",
    "ServiceCallJournalUnavailableError",
    "ServiceCallOwnershipLostError",
    "service_call_journal_root",
]
