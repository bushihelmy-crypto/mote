"""Single command owner for the durable Workflow run lifecycle."""

from __future__ import annotations

import hashlib

from mote.contracts.runtime.operation_ownership import (
    EffectCapability,
    OperationBackend,
    OperationOwnership,
    OperationOwnershipRequest,
)
from mote.contracts.workflow.identity import WorkflowRunId

from .model import (
    CheckpointWorkflowRun,
    CreateWorkflowRun,
    PauseWorkflowRun,
    ResumeWorkflowRun,
    SettleWorkflowRun,
    WorkflowRunCommand,
    WorkflowRunPhase,
    WorkflowRunProjection,
)
from .store import WorkflowRunStore


class WorkflowRunControl:
    def __init__(
        self,
        store: WorkflowRunStore,
        ownership,
        *,
        deployment_id: str,
        holder_id: str,
        backend: OperationBackend,
        lease_ttl_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._ownership = ownership
        self._deployment_id = deployment_id
        self._holder_id = holder_id
        self._backend = backend
        self._lease_ttl_seconds = lease_ttl_seconds

    def create(
        self,
        command: CreateWorkflowRun,
        *,
        admission_ownership: OperationOwnership | None = None,
    ) -> WorkflowRunProjection:
        ownership = admission_ownership if admission_ownership is not None else self._claim(command.reference.run_id, 0)
        owns_claim = admission_ownership is None
        try:
            current = self._store.get(command.reference)
            if current is not None:
                if (
                    current.request_id != command.request_id
                    or current.reference != command.reference
                    or current.provenance != command.provenance
                    or current.access_grant != command.access_grant
                    or current.definition_source != command.definition_source
                    or current.definition_digest != command.definition_digest
                    or current.initial_input_payload != command.initial_input_payload
                ):
                    raise RuntimeError("workflow request identity conflicts with an existing run")
                return current
            projection = WorkflowRunProjection(
                command.reference,
                command.request_id,
                command.provenance,
                command.access_grant,
                1,
                WorkflowRunPhase.CREATED,
                command.checkpoint_payload,
                command.frontier,
                command.deadline,
                None,
                "",
                None,
                command.definition_source,
                command.definition_digest,
                command.initial_input_payload,
            )
            if admission_ownership is not None:
                return self._store.commit_create(projection, ownership=ownership)
            return self._store.commit(projection, expected_revision=None, ownership=ownership)
        finally:
            if owns_claim:
                self._ownership.release(ownership)

    def start(
        self,
        command: WorkflowRunCommand,
        *,
        execution_ownership: OperationOwnership | None = None,
    ) -> WorkflowRunProjection:
        return self._transition(
            command,
            {WorkflowRunPhase.CREATED},
            WorkflowRunPhase.RUNNING,
            execution_ownership=execution_ownership,
        )

    def cancel(self, command: WorkflowRunCommand) -> WorkflowRunProjection:
        return self._transition(
            command,
            {WorkflowRunPhase.CREATED, WorkflowRunPhase.RUNNING, WorkflowRunPhase.PAUSED},
            WorkflowRunPhase.CANCELLING,
        )

    def checkpoint(
        self,
        command: CheckpointWorkflowRun,
        *,
        execution_ownership: OperationOwnership | None = None,
    ) -> WorkflowRunProjection:
        ownership, current = self._owned_current(command)
        try:
            self._expect(current, command.expected_revision, {WorkflowRunPhase.RUNNING})
            updated = WorkflowRunProjection(
                current.reference,
                current.request_id,
                current.provenance,
                current.access_grant,
                current.revision + 1,
                current.phase,
                command.checkpoint_payload,
                command.frontier,
                current.deadline,
                current.pause_reason,
                current.resume_nonce,
                current.terminal_result,
                current.definition_source,
                current.definition_digest,
                current.initial_input_payload,
            )
            return self._store.commit(
                updated,
                expected_revision=current.revision,
                ownership=ownership,
                execution_ownership=execution_ownership,
            )
        finally:
            self._ownership.release(ownership)

    def pause(
        self,
        command: PauseWorkflowRun,
        *,
        execution_ownership: OperationOwnership | None = None,
    ) -> WorkflowRunProjection:
        ownership, current = self._owned_current(command)
        try:
            self._expect(current, command.expected_revision, {WorkflowRunPhase.RUNNING})
            nonce = hashlib.sha256(f"{current.reference.run_id}:{current.revision + 1}".encode()).hexdigest()
            updated = WorkflowRunProjection(
                current.reference,
                current.request_id,
                current.provenance,
                current.access_grant,
                current.revision + 1,
                WorkflowRunPhase.PAUSED,
                command.checkpoint_payload,
                command.frontier,
                current.deadline,
                command.reason,
                nonce,
                None,
                current.definition_source,
                current.definition_digest,
                current.initial_input_payload,
            )
            return self._store.commit(
                updated,
                expected_revision=current.revision,
                ownership=ownership,
                execution_ownership=execution_ownership,
            )
        finally:
            self._ownership.release(ownership)

    def resume(
        self,
        command: ResumeWorkflowRun,
        *,
        execution_ownership: OperationOwnership | None = None,
    ) -> WorkflowRunProjection:
        ownership, current = self._owned_current(command)
        try:
            self._expect(current, command.expected_revision, {WorkflowRunPhase.PAUSED})
            if command.resume_nonce != current.resume_nonce:
                raise RuntimeError("workflow resume token is stale or foreign")
            updated = WorkflowRunProjection(
                current.reference,
                current.request_id,
                current.provenance,
                current.access_grant,
                current.revision + 1,
                WorkflowRunPhase.RUNNING,
                command.checkpoint_payload,
                command.frontier,
                current.deadline,
                None,
                "",
                None,
                current.definition_source,
                current.definition_digest,
                current.initial_input_payload,
            )
            return self._store.commit(
                updated,
                expected_revision=current.revision,
                ownership=ownership,
                execution_ownership=execution_ownership,
            )
        finally:
            self._ownership.release(ownership)

    def settle(
        self,
        command: SettleWorkflowRun,
        *,
        execution_ownership: OperationOwnership | None = None,
    ) -> WorkflowRunProjection:
        if command.phase not in {
            WorkflowRunPhase.SUCCEEDED,
            WorkflowRunPhase.FAILED,
            WorkflowRunPhase.CANCELLED,
            WorkflowRunPhase.TIMED_OUT,
        }:
            raise ValueError("workflow settlement phase must be terminal")
        ownership, current = self._owned_current(command)
        try:
            allowed = (
                {WorkflowRunPhase.CANCELLING}
                if command.phase is WorkflowRunPhase.CANCELLED
                else {WorkflowRunPhase.RUNNING}
            )
            self._expect(current, command.expected_revision, allowed)
            updated = WorkflowRunProjection(
                current.reference,
                current.request_id,
                current.provenance,
                current.access_grant,
                current.revision + 1,
                command.phase,
                current.checkpoint_payload,
                (),
                current.deadline,
                None,
                "",
                command.terminal_result,
                current.definition_source,
                current.definition_digest,
                current.initial_input_payload,
            )
            return self._store.commit(
                updated,
                expected_revision=current.revision,
                ownership=ownership,
                execution_ownership=execution_ownership,
            )
        finally:
            self._ownership.release(ownership)

    def _transition(self, command, allowed, phase, *, execution_ownership=None):
        ownership, current = self._owned_current(command)
        try:
            self._expect(current, command.expected_revision, allowed)
            updated = WorkflowRunProjection(
                current.reference,
                current.request_id,
                current.provenance,
                current.access_grant,
                current.revision + 1,
                phase,
                current.checkpoint_payload,
                current.frontier,
                current.deadline,
                current.pause_reason,
                current.resume_nonce,
                current.terminal_result,
                current.definition_source,
                current.definition_digest,
                current.initial_input_payload,
            )
            return self._store.commit(
                updated,
                expected_revision=current.revision,
                ownership=ownership,
                execution_ownership=execution_ownership,
            )
        finally:
            self._ownership.release(ownership)

    def _owned_current(self, command):
        ownership = self._claim(command.reference.run_id, command.expected_revision)
        current = self._store.get(command.reference)
        if current is None:
            self._ownership.release(ownership)
            raise KeyError(command.reference.run_id)
        return ownership, current

    def _claim(self, run_id: WorkflowRunId, revision: int) -> OperationOwnership:
        operation_id = str(run_id)
        return self._ownership.claim(
            OperationOwnershipRequest(
                self._deployment_id,
                operation_id,
                self._holder_id,
                self._backend,
                revision,
                f"workflow:{operation_id}",
                EffectCapability.RECONCILABLE_BY_RECEIPT,
            ),
            self._lease_ttl_seconds,
        )

    @staticmethod
    def _expect(current, revision, allowed) -> None:
        if current.revision != revision or current.phase not in allowed:
            raise RuntimeError("workflow command conflicts with canonical state")


__all__ = ["WorkflowRunControl"]
