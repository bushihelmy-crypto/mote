"""Crash-safe local journal for logical model calls and wire attempts."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from mote.contracts.model.checkpoint import ModelCheckpointAdmissionDisposition, ModelCheckpointPolicy
from mote.contracts.model.failover import AttemptState, ModelCallState
from mote.contracts.model.model_journal import (
    ModelAttemptFinishedRecord,
    ModelAttemptStartedRecord,
    ModelCallFinishedRecord,
    ModelCallJournalRecord,
    ModelCallPlannedRecord,
    ModelCallRecovery,
    ModelDecisionRecord,
    ModelWireAuthorizedRecord,
)
from mote.contracts.model.topology_codec import decode_route_id
from mote.runtime.persistence import disk_io
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.persistence.transaction_gate import SerialTransactionGate
from mote.runtime.telemetry.logging import log_class

MODEL_CALL_JOURNAL_DIRNAME = "model-calls"
_RECORD_ADAPTER = TypeAdapter(ModelCallJournalRecord)


def validate_model_call_record_stream(
    records: tuple[ModelCallJournalRecord, ...],
) -> None:
    """Validate one decoded stream without acquiring store or writer authority."""

    LocalModelCallJournal._validate_stream(records)


class ModelCallJournalError(RuntimeError):
    pass


class ModelCallJournalIntegrityError(ModelCallJournalError):
    pass


class ModelCallJournalUnavailableError(ModelCallJournalError):
    pass


class ModelCallCapacityError(ModelCallJournalError):
    disposition = ModelCheckpointAdmissionDisposition.LIMIT_EXCEEDED


@log_class(level="DEBUG", exclude={"path_for"})
class LocalModelCallJournal:
    """One append-only fsynced JSONL stream per opaque model call ID."""

    def __init__(self, root: str | Path, *, policy: ModelCheckpointPolicy) -> None:
        self._root = Path(root)
        self._policy = policy
        self._transactions = SerialTransactionGate()

    def path_for(self, model_call_id: str) -> Path:
        if type(model_call_id) is not str or not model_call_id:
            raise ValueError("model_call_id must be a non-empty string")
        digest = hashlib.sha256(model_call_id.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.jsonl"

    async def append(self, record: ModelCallJournalRecord) -> None:
        task = asyncio.create_task(run_disk_io(self.append_committed, record))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    def append_committed(self, record: ModelCallJournalRecord) -> None:
        path = self.path_for(record.model_call_id)
        with self._transactions.transaction():
            existing = self._read_path(path, expected_call_id=record.model_call_id)
            if not existing and isinstance(record, ModelCallPlannedRecord):
                self._validate_global_admission()
            self._validate_append(existing, record)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                disk_io.append_line(
                    path,
                    record.model_dump_json(),
                    mode=0o600,
                )
            except OSError as exc:
                raise ModelCallJournalUnavailableError("model-call journal cannot be appended") from exc

    def records(self, model_call_id: str) -> tuple[ModelCallJournalRecord, ...]:
        path = self.path_for(model_call_id)
        with self._transactions.transaction():
            return self._read_path(path, expected_call_id=model_call_id)

    def recover(self, model_call_id: str) -> ModelCallRecovery:
        records = self.records(model_call_id)
        return self._recover_records(records)

    def in_doubt(self) -> tuple[ModelCallRecovery, ...]:
        if not self._root.exists():
            return ()
        recoveries: list[ModelCallRecovery] = []
        with self._transactions.transaction():
            paths = tuple(sorted(self._root.glob("*.jsonl")))
            record_sets = tuple(self._read_path(path, expected_call_id=None) for path in paths)
        for records in record_sets:
            recovery = self._recover_records(records)
            if recovery.state is ModelCallState.IN_DOUBT:
                recoveries.append(recovery)
        return tuple(recoveries)

    def _validate_global_admission(self) -> None:
        if not self._root.exists():
            return
        active = 0
        for path in sorted(self._root.glob("*.jsonl")):
            records = self._read_path(path, expected_call_id=None)
            if not records:
                continue
            recovery = self._recover_records(records)
            if recovery.state in {
                ModelCallState.PLANNED,
                ModelCallState.RUNNING,
                ModelCallState.IN_DOUBT,
            }:
                active += 1
                if active >= self._policy.active_global:
                    raise ModelCallCapacityError("ModelCall global active capacity is exhausted")

    @staticmethod
    def _read_path(
        path: Path,
        *,
        expected_call_id: str | None,
    ) -> tuple[ModelCallJournalRecord, ...]:
        if not path.exists():
            return ()
        records: list[ModelCallJournalRecord] = []
        try:
            with path.open("rb") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.endswith(b"\n"):
                        raise ModelCallJournalIntegrityError(f"model-call journal line {line_number} is incomplete")
                    try:
                        record = _RECORD_ADAPTER.validate_json(line)
                    except ValidationError as exc:
                        raise ModelCallJournalIntegrityError(
                            f"model-call journal line {line_number} is invalid"
                        ) from exc
                    if expected_call_id is not None and record.model_call_id != expected_call_id:
                        raise ModelCallJournalIntegrityError(
                            "model-call journal file identity does not match its record"
                        )
                    records.append(record)
        except OSError as exc:
            raise ModelCallJournalIntegrityError("model-call journal cannot be read") from exc
        LocalModelCallJournal._validate_stream(tuple(records))
        return tuple(records)

    @staticmethod
    def _validate_append(
        existing: tuple[ModelCallJournalRecord, ...],
        record: ModelCallJournalRecord,
    ) -> None:
        if not existing:
            if not isinstance(record, ModelCallPlannedRecord):
                raise ModelCallJournalIntegrityError("model-call journal must begin with call_planned")
            return
        if isinstance(existing[-1], ModelCallFinishedRecord):
            raise ModelCallJournalIntegrityError("model call is already terminal")
        LocalModelCallJournal._validate_stream(existing + (record,))

    @staticmethod
    def _validate_stream(records: tuple[ModelCallJournalRecord, ...]) -> None:
        if not records:
            return
        first_plan = records[0]
        if not isinstance(first_plan, ModelCallPlannedRecord):
            raise ModelCallJournalIntegrityError("model-call journal must begin with call_planned")
        if first_plan.resume_generation != 0:
            raise ModelCallJournalIntegrityError("model-call journal first plan must be generation zero")
        call_id = first_plan.model_call_id
        current_plan = first_plan
        started: dict[str, ModelAttemptStartedRecord] = {}
        finished: set[str] = set()
        authorized: set[str] = set()
        terminal = False
        for record_index, record in enumerate(records[1:], start=2):
            if record.model_call_id != call_id:
                raise ModelCallJournalIntegrityError("model-call journal contains mixed call identities")
            if terminal:
                raise ModelCallJournalIntegrityError("model-call journal contains records after terminal")
            if isinstance(record, ModelCallPlannedRecord):
                if set(started) != finished:
                    raise ModelCallJournalIntegrityError("resume generation began with an open attempt")
                if record.resume_generation != current_plan.resume_generation + 1:
                    raise ModelCallJournalIntegrityError("resume generation is not contiguous")
                current_plan = record
            elif isinstance(record, ModelAttemptStartedRecord):
                if record.attempt_id in started or record.ordinal != len(started) + 1:
                    raise ModelCallJournalIntegrityError("model attempt start identity or ordinal is inconsistent")
                if (
                    record.resume_generation != current_plan.resume_generation
                    or record.endpoint_id not in current_plan.endpoint_ids
                ):
                    raise ModelCallJournalIntegrityError("model attempt does not belong to the active generation")
                started[record.attempt_id] = record
            elif isinstance(record, ModelAttemptFinishedRecord):
                start = started.get(record.attempt_id)
                if (
                    start is None
                    or record.attempt_id in finished
                    or record.ordinal != start.ordinal
                    or record.resume_generation != start.resume_generation
                ):
                    raise ModelCallJournalIntegrityError("model attempt terminal does not match one open attempt")
                finished.add(record.attempt_id)
            elif isinstance(record, ModelWireAuthorizedRecord):
                start = started.get(record.attempt_id)
                if (
                    start is None
                    or record.attempt_id in authorized
                    or record.attempt_id in finished
                    or record.ordinal != start.ordinal
                    or record.resume_generation != start.resume_generation
                    or record.issued_journal_revision != record_index
                ):
                    raise ModelCallJournalIntegrityError("wire authorization does not match one open attempt")
                authorized.add(record.attempt_id)
            elif isinstance(record, ModelDecisionRecord):
                if record.resume_generation != current_plan.resume_generation or record.after_attempt_ordinal > len(
                    started
                ):
                    raise ModelCallJournalIntegrityError("model decision does not belong to the active generation")
            elif isinstance(record, ModelCallFinishedRecord):
                if set(started) != finished:
                    raise ModelCallJournalIntegrityError("model call became terminal with an open attempt")
                terminal = True

    @staticmethod
    def _recover_records(
        records: tuple[ModelCallJournalRecord, ...],
    ) -> ModelCallRecovery:
        if not records or not isinstance(records[0], ModelCallPlannedRecord):
            raise ModelCallJournalIntegrityError("model call has no plan record")
        plans = tuple(record for record in records if isinstance(record, ModelCallPlannedRecord))
        for current_plan in plans:
            decode_route_id(current_plan.route_id)
        plan = plans[-1]
        starts = {record.attempt_id: record for record in records if isinstance(record, ModelAttemptStartedRecord)}
        finish_records = {
            record.attempt_id: record for record in records if isinstance(record, ModelAttemptFinishedRecord)
        }
        terminal = next(
            (record for record in reversed(records) if isinstance(record, ModelCallFinishedRecord)),
            None,
        )
        open_attempts = tuple(
            start.attempt_id
            for start in sorted(starts.values(), key=lambda item: item.ordinal)
            if start.attempt_id not in finish_records
        )
        in_doubt = tuple(
            start.attempt_id
            for start in sorted(starts.values(), key=lambda item: item.ordinal)
            if start.attempt_id in open_attempts or finish_records[start.attempt_id].state is AttemptState.IN_DOUBT
        )
        if terminal is not None:
            state = terminal.state
        elif open_attempts:
            state = ModelCallState.IN_DOUBT
        elif any(finish.state is AttemptState.IN_DOUBT for finish in finish_records.values()) and not any(
            start.resume_generation == plan.resume_generation for start in starts.values()
        ):
            state = ModelCallState.IN_DOUBT
        elif any(start.resume_generation == plan.resume_generation for start in starts.values()):
            state = ModelCallState.RUNNING
        else:
            state = ModelCallState.PLANNED
        return ModelCallRecovery(
            model_call_id=plan.model_call_id,
            state=state,
            plan=plan,
            original_plan=plans[0],
            plans=plans,
            attempts_started=len(starts),
            attempts_finished=len(finish_records),
            in_doubt_attempt_ids=in_doubt,
            attempt_starts=tuple(sorted(starts.values(), key=lambda item: item.ordinal)),
            attempt_finishes=tuple(
                finish_records[start.attempt_id]
                for start in sorted(starts.values(), key=lambda item: item.ordinal)
                if start.attempt_id in finish_records
            ),
            decisions=tuple(record for record in records if isinstance(record, ModelDecisionRecord)),
            terminal=terminal,
        )


def model_call_journal_root(workspace_root: Path) -> Path:
    return workspace_root / ".runtime" / MODEL_CALL_JOURNAL_DIRNAME


__all__ = [
    "LocalModelCallJournal",
    "MODEL_CALL_JOURNAL_DIRNAME",
    "ModelCallJournalError",
    "ModelCallJournalIntegrityError",
    "ModelCallJournalUnavailableError",
    "ModelCallCapacityError",
    "model_call_journal_root",
    "validate_model_call_record_stream",
]
