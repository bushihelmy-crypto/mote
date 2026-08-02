"""Strict, atomic durable Workflow run store."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from mote.contracts.clock import AbsoluteInstant
from mote.contracts.ports.runtime.operation_ownership import OperationOwnershipPort
from mote.contracts.runtime.operation_ownership import EffectCapability, OperationOwnership
from mote.contracts.workflow import (
    WorkflowDefinitionId,
    WorkflowRunId,
    WorkflowRunReference,
    decode_workflow_access_grant,
    decode_workflow_definition_source,
    decode_workflow_provenance,
    decode_workflow_terminal_result,
    encode_workflow_access_grant,
    encode_workflow_definition_source,
    encode_workflow_provenance,
    encode_workflow_terminal_result,
)

from .model import WorkflowPauseReason, WorkflowRunPhase, WorkflowRunProjection

_SCHEMA = "mote.workflow-run-store/v3"


class WorkflowRunStore:
    def __init__(self, path: Path, ownership: OperationOwnershipPort) -> None:
        self._path = path
        self._ownership = ownership

    def get(self, reference: WorkflowRunReference) -> WorkflowRunProjection | None:
        current = self._read().get(reference.run_id)
        if current is not None and current.reference != reference:
            raise RuntimeError("workflow definition identity mismatch")
        return current

    def scan(self) -> tuple[WorkflowRunProjection, ...]:
        return tuple(sorted(self._read().values(), key=lambda item: item.reference.run_id))

    def commit(
        self,
        projection: WorkflowRunProjection,
        *,
        expected_revision: int | None,
        ownership: OperationOwnership,
        execution_ownership: OperationOwnership | None = None,
    ) -> WorkflowRunProjection:
        if ownership.request.operation_id != projection.reference.run_id:
            raise RuntimeError("workflow ownership does not bind this run")
        if (
            execution_ownership is not None
            and execution_ownership.request.operation_id != f"workflow-execution:{projection.reference.run_id}"
        ):
            raise RuntimeError("workflow execution ownership does not bind this run")
        ownerships = (ownership,) if execution_ownership is None else (ownership, execution_ownership)
        return self._commit_guarded(projection, expected_revision, ownerships)

    def _commit_guarded(
        self,
        projection: WorkflowRunProjection,
        expected_revision: int | None,
        ownerships: tuple[OperationOwnership, ...],
    ) -> WorkflowRunProjection:
        with self._ownership.guard_many(ownerships):
            with self._store_transaction():
                records = self._read()
                current = records.get(projection.reference.run_id)
                actual = None if current is None else current.revision
                if actual != expected_revision:
                    raise RuntimeError("workflow run revision conflict")
                records[projection.reference.run_id] = projection
                self._write(records)
                return projection

    def commit_create(
        self,
        projection: WorkflowRunProjection,
        *,
        ownership: OperationOwnership,
    ) -> WorkflowRunProjection:
        """Commit the initial run fact under its create-admission fence."""
        if (
            ownership.request.operation_id != str(projection.provenance.workflow_create_admission_id)
            or ownership.request.effect_capability is not EffectCapability.NO_EXTERNAL_EFFECT
        ):
            raise RuntimeError("workflow create ownership does not bind admission")
        with self._ownership.guard(ownership):
            with self._store_transaction():
                records = self._read()
                current = records.get(projection.reference.run_id)
                if current is not None:
                    if current != projection:
                        raise RuntimeError("workflow request identity conflicts with an existing run")
                    return current
                records[projection.reference.run_id] = projection
                self._write(records)
                return projection

    @contextmanager
    def _store_transaction(self) -> Iterator[None]:
        """Serialize the read/modify/replace transaction across all run ids."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(f"{self._path.suffix}.lock")
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, WorkflowRunProjection]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if (
            type(raw) is not dict
            or set(raw) != {"schema", "runs"}
            or raw["schema"] != _SCHEMA
            or type(raw["runs"]) is not list
        ):
            raise ValueError("workflow run store envelope is invalid")
        records: dict[str, WorkflowRunProjection] = {}
        fields = {
            "run_id",
            "request_id",
            "definition_id",
            "provenance",
            "access_grant",
            "revision",
            "phase",
            "checkpoint_payload",
            "frontier",
            "deadline",
            "pause_reason",
            "resume_nonce",
            "terminal_result",
            "definition_source",
            "definition_digest",
            "initial_input_payload",
        }
        for item in raw["runs"]:
            if type(item) is not dict or set(item) != fields:
                raise ValueError("workflow run record shape is invalid")
            for key in (
                "run_id",
                "request_id",
                "definition_id",
                "checkpoint_payload",
                "resume_nonce",
                "definition_digest",
                "initial_input_payload",
            ):
                if type(item[key]) is not str:
                    raise ValueError("workflow run string field is invalid")
            if not item["run_id"] or not item["request_id"] or not item["definition_id"]:
                raise ValueError("workflow run identity is invalid")
            if type(item["revision"]) is not int or item["revision"] < 1:
                raise ValueError("workflow run revision is invalid")
            if type(item["frontier"]) is not list or any(
                type(value) is not str or not value for value in item["frontier"]
            ):
                raise ValueError("workflow frontier is invalid")
            deadline = None if item["deadline"] is None else AbsoluteInstant.from_dict(item["deadline"])
            pause_reason = None if item["pause_reason"] is None else WorkflowPauseReason(item["pause_reason"])
            terminal_result = (
                None if item["terminal_result"] is None else decode_workflow_terminal_result(item["terminal_result"])
            )
            reference = WorkflowRunReference(WorkflowRunId(item["run_id"]), WorkflowDefinitionId(item["definition_id"]))
            if terminal_result is not None and terminal_result.run_id != reference.run_id:
                raise ValueError("workflow terminal result binds another run")
            record = WorkflowRunProjection(
                reference,
                item["request_id"],
                decode_workflow_provenance(item["provenance"]),
                decode_workflow_access_grant(item["access_grant"]),
                item["revision"],
                WorkflowRunPhase(item["phase"]),
                item["checkpoint_payload"],
                tuple(item["frontier"]),
                deadline,
                pause_reason,
                item["resume_nonce"],
                terminal_result,
                decode_workflow_definition_source(item["definition_source"]),
                item["definition_digest"],
                item["initial_input_payload"],
            )
            if record.reference.run_id in records:
                raise ValueError("duplicate workflow run identity")
            records[record.reference.run_id] = record
        return records

    def _write(self, records: dict[str, WorkflowRunProjection]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": _SCHEMA,
            "runs": [
                {
                    "run_id": item.reference.run_id,
                    "request_id": item.request_id,
                    "definition_id": item.reference.definition_id,
                    "revision": item.revision,
                    "provenance": encode_workflow_provenance(item.provenance),
                    "access_grant": encode_workflow_access_grant(item.access_grant),
                    "phase": item.phase.value,
                    "checkpoint_payload": item.checkpoint_payload,
                    "frontier": list(item.frontier),
                    "deadline": None if item.deadline is None else item.deadline.to_dict(),
                    "pause_reason": None if item.pause_reason is None else item.pause_reason.value,
                    "resume_nonce": item.resume_nonce,
                    "terminal_result": (
                        None if item.terminal_result is None else encode_workflow_terminal_result(item.terminal_result)
                    ),
                    "definition_source": encode_workflow_definition_source(item.definition_source),
                    "definition_digest": item.definition_digest,
                    "initial_input_payload": item.initial_input_payload,
                }
                for item in sorted(records.values(), key=lambda value: value.reference.run_id)
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


__all__ = ["WorkflowRunStore"]
