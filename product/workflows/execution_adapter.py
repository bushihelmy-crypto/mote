"""Product adapter between Workflow execution and its durable run owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict
from enum import Enum

from pydantic import BaseModel

from mote.contracts.artifact import (
    ArtifactPublishRequest,
    ArtifactRepresentationInput,
    ArtifactRetention,
    ArtifactSensitivity,
)
from mote.contracts.events.task import TaskProgressEvent
from mote.contracts.ports.artifact.store import ReliableArtifactPublisher
from mote.contracts.runtime.operation_ownership import OperationOwnership
from mote.contracts.task.progress import ActivityProgressEvent, DurableWorkflowRunProgress, ProgressEvent
from mote.contracts.workflow import (
    WorkflowCancelled,
    WorkflowFailed,
    WorkflowSucceededArtifact,
    WorkflowSucceededInline,
    WorkflowTerminalResult,
    WorkflowTimedOut,
    encode_workflow_terminal_result,
)
from mote.contracts.workflow.result import MAX_WORKFLOW_INLINE_RESULT_BYTES
from mote.orchestration.workflows import Cancelled, Failed, Paused, Succeeded, TimedOut, WorkflowOutcome, WorkflowRun
from mote.orchestration.workflows.durable import (
    CheckpointWorkflowRun,
    PauseWorkflowRun,
    ResumeWorkflowRun,
    SettleWorkflowRun,
    WorkflowPauseReason,
    WorkflowRunCommand,
    WorkflowRunPhase,
    WorkflowRunProjection,
)
from mote.product.workflows.durability import ProductWorkflowDurability
from mote.runtime.events.context import observe_event_sync


async def _terminal_result(
    projection: WorkflowRunProjection,
    outcome: WorkflowOutcome,
    artifacts: ReliableArtifactPublisher,
) -> WorkflowTerminalResult:
    if isinstance(outcome, Succeeded):
        encoded = json.dumps(
            _json_value(outcome.output),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) <= MAX_WORKFLOW_INLINE_RESULT_BYTES:
            terminal = WorkflowSucceededInline(encoded.decode("utf-8"))
        else:
            digest = hashlib.sha256(encoded).hexdigest()
            publication_id = f"workflow-result-{projection.reference.run_id}-" f"{projection.revision + 1}-{digest}"
            revision = await artifacts.publish(
                publication_id,
                ArtifactPublishRequest(
                    idempotency_key=publication_id,
                    retention=ArtifactRetention.SESSION,
                    sensitivity=ArtifactSensitivity.PRIVATE,
                    representations=(
                        ArtifactRepresentationInput(
                            representation="canonical",
                            kind="workflow_result",
                            mime_type="application/json",
                            content=encoded,
                            suggested_name=f"{projection.reference.run_id}.json",
                        ),
                    ),
                ),
            )
            terminal = WorkflowSucceededArtifact(revision.get("canonical"))
    elif isinstance(outcome, Failed):
        terminal = WorkflowFailed(type(outcome.error).__name__, str(outcome.error))
    elif isinstance(outcome, TimedOut):
        terminal = WorkflowTimedOut(str(outcome.reason))
    elif isinstance(outcome, Cancelled):
        terminal = WorkflowCancelled(str(outcome.reason))
    else:
        raise TypeError("paused Workflow outcome is not terminal")
    return WorkflowTerminalResult(projection.reference.run_id, projection.revision + 1, terminal)


def _terminal_payload(projection: WorkflowRunProjection) -> str:
    if projection.terminal_result is None:
        raise RuntimeError("terminal Workflow projection has no terminal result")
    return json.dumps(
        encode_workflow_terminal_result(projection.terminal_result),
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_value(value):
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_json_value(item) for item in value)
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Workflow result contains a non-finite float")
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise TypeError(f"Workflow checkpoint contains {type(value).__name__}")


class WorkflowExecutionAdapter:
    """Drive one WorkflowRun through its canonical durable control."""

    def __init__(
        self,
        run: WorkflowRun,
        durability: ProductWorkflowDurability,
        projection: WorkflowRunProjection,
        artifact_publisher: ReliableArtifactPublisher,
    ) -> None:
        self._run = run
        self._durability = durability
        self._projection = projection
        self._artifact_publisher = artifact_publisher
        self._task: asyncio.Task[WorkflowOutcome] | None = None
        self._closed = False
        self._terminal: WorkflowOutcome | None = None
        self._terminal_lock = asyncio.Lock()
        self._terminal_destination = ""
        self._execution_ownership: OperationOwnership | None = None
        self._run.bind_checkpoint_sink(self)
        self._run.bind_progress_sink(self)

    def emit(self, event: ProgressEvent) -> None:
        if not isinstance(event, ActivityProgressEvent):
            raise TypeError("durable Workflow sink requires activity progress")
        ownership = self._execution_ownership
        if ownership is None:
            return
        if event.identity.definition_id != str(self._projection.reference.definition_id):
            raise RuntimeError("Workflow progress definition identity mismatch")
        self._durability.assert_execution_current(self._projection.reference.run_id, ownership)
        observe_event_sync(
            TaskProgressEvent(
                DurableWorkflowRunProgress(
                    self._projection.reference,
                    self._projection.revision,
                    event.stage,
                    event.phase,
                    event.detail,
                )
            )
        )

    async def commit_checkpoint(self, state, run_state, frontier) -> None:
        if self._execution_ownership is None:
            raise RuntimeError("Workflow checkpoint has no execution owner")
        payload = json.dumps(
            {
                "schema": "mote.workflow-checkpoint/v2",
                "state": _json_value(state),
                "run_state": _json_value(asdict(run_state)),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._projection = self._durability.control.checkpoint(
            CheckpointWorkflowRun(
                self._projection.reference,
                self._projection.revision,
                payload,
                frontier,
            ),
            execution_ownership=self._execution_ownership,
        )

    def bind_terminal_destination(self, destination_id: str) -> None:
        if not destination_id or self._terminal_destination:
            raise ValueError("Workflow terminal destination is invalid or already bound")
        self._terminal_destination = destination_id

    async def execute(self, execution_ownership: OperationOwnership) -> WorkflowOutcome:
        async with self._terminal_lock:
            if self._terminal is not None:
                return self._terminal
        if self._closed or self._task is not None:
            raise RuntimeError("WorkflowExecutionAdapter instances are single-use")
        expected_operation_id = f"workflow-execution:{self._projection.reference.run_id}"
        if execution_ownership.request.operation_id != expected_operation_id:
            raise RuntimeError("Workflow execution ownership binds another run")
        self._execution_ownership = execution_ownership
        if self._projection.phase is WorkflowRunPhase.CREATED:
            self._projection = self._durability.control.start(
                WorkflowRunCommand(self._projection.reference, self._projection.revision),
                execution_ownership=execution_ownership,
            )
        elif self._projection.phase is WorkflowRunPhase.PAUSED:
            self._projection = self._durability.control.resume(
                ResumeWorkflowRun(
                    self._projection.reference,
                    self._projection.revision,
                    self._projection.resume_nonce,
                    self._projection.checkpoint_payload,
                    self._projection.frontier,
                ),
                execution_ownership=execution_ownership,
            )
        elif self._projection.phase not in {
            WorkflowRunPhase.RUNNING,
            WorkflowRunPhase.CANCELLING,
        }:
            raise RuntimeError("terminal Workflow run cannot execute again")
        if self._projection.phase is WorkflowRunPhase.CANCELLING:
            outcome: WorkflowOutcome = Cancelled()
        else:
            self._task = asyncio.create_task(self._run.execute())
            outcome = await self._task
        current = self._durability.query(self._projection.reference)
        if current is None:
            raise RuntimeError("durable Workflow projection disappeared during execution")
        if current.phase is WorkflowRunPhase.CANCELLING:
            self._projection = current
            outcome = Cancelled()
        elif current.revision != self._projection.revision:
            raise RuntimeError("durable Workflow projection changed during execution")
        phase = {
            Succeeded: WorkflowRunPhase.SUCCEEDED,
            Failed: WorkflowRunPhase.FAILED,
            TimedOut: WorkflowRunPhase.TIMED_OUT,
            Cancelled: WorkflowRunPhase.CANCELLED,
        }.get(type(outcome))
        if isinstance(outcome, Paused):
            snapshot = self._run.snapshot()
            self._projection = self._durability.control.pause(
                PauseWorkflowRun(
                    self._projection.reference,
                    self._projection.revision,
                    WorkflowPauseReason.OPERATOR,
                    json.dumps(
                        _json_value(snapshot.state),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    (),
                ),
                execution_ownership=execution_ownership,
            )
        elif phase is not None and not self._projection.phase.terminal:
            self._projection = self._durability.control.settle(
                SettleWorkflowRun(
                    self._projection.reference,
                    self._projection.revision,
                    phase,
                    await _terminal_result(self._projection, outcome, self._artifact_publisher),
                ),
                execution_ownership=execution_ownership,
            )
        if self._projection.phase.terminal:
            if not self._terminal_destination:
                raise RuntimeError("Workflow terminal destination is not bound")
            self._durability.reconciler.submit_terminal(
                self._projection.reference.run_id,
                self._terminal_destination,
                _terminal_payload(self._projection),
            )
        async with self._terminal_lock:
            if self._terminal is None:
                self._terminal = outcome
            return self._terminal

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._run.aclose()


__all__ = ["WorkflowExecutionAdapter"]
