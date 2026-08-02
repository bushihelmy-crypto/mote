"""Workflow progress facts emitted through a run-scoped sink."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from mote.contracts.task.progress import (
    ActivityProgressEvent,
    ActivityProgressIdentity,
    ProgressEventSink,
    ProgressPhase,
)
from mote.contracts.workflow.identity import WorkflowDefinitionId
from mote.orchestration.workflows.types import GraphRunState, GraphState, WorkflowNodeStatus
from mote.runtime.events.progress_scope import bind_progress_sink, current_progress_sink, reset_progress_sink
from mote.runtime.telemetry.logging import logger


@dataclass(frozen=True, slots=True)
class ActivityRunStarted:
    type: Literal["activity_run_started"] = "activity_run_started"
    schema_version: int = 1
    activity_run_id: str = ""
    activity_definition_id: str = ""


@dataclass(frozen=True, slots=True)
class ActivityRunTerminal:
    type: Literal["activity_run_terminal"] = "activity_run_terminal"
    schema_version: int = 1
    activity_run_id: str = ""
    status: str = ""


WorkflowEvent: TypeAlias = ActivityRunStarted | ActivityRunTerminal


class ProgressSink(Protocol):
    async def emit(self, event: WorkflowEvent) -> None: ...


class WorkflowCheckpointSink(Protocol):
    async def commit_checkpoint(
        self,
        state: GraphState,
        run_state: GraphRunState,
        frontier: tuple[str, ...],
    ) -> None: ...


_checkpoint_sink: contextvars.ContextVar[WorkflowCheckpointSink | None] = contextvars.ContextVar(
    "mote_workflow_checkpoint_sink", default=None
)


def set_checkpoint_sink(
    sink: WorkflowCheckpointSink | None,
) -> contextvars.Token[WorkflowCheckpointSink | None]:
    return _checkpoint_sink.set(sink)


def reset_checkpoint_sink(
    token: contextvars.Token[WorkflowCheckpointSink | None],
) -> None:
    _checkpoint_sink.reset(token)


async def commit_workflow_checkpoint(
    state: GraphState,
    run_state: GraphRunState,
    frontier: tuple[str, ...],
) -> None:
    sink = _checkpoint_sink.get()
    if sink is not None:
        await sink.commit_checkpoint(state, run_state, frontier)


_PHASES = {
    WorkflowNodeStatus.RUNNING: ProgressPhase.RUNNING,
    WorkflowNodeStatus.SUCCESS: ProgressPhase.SUCCESS,
    WorkflowNodeStatus.FAILED: ProgressPhase.FAILED,
    WorkflowNodeStatus.CANCELLED: ProgressPhase.CANCELLED,
    WorkflowNodeStatus.TIMEOUT: ProgressPhase.TIMEOUT,
    WorkflowNodeStatus.SKIPPED: ProgressPhase.SKIPPED,
    WorkflowNodeStatus.WAITING_FOR_ROUTE: ProgressPhase.WAITING_FOR_ROUTE,
    WorkflowNodeStatus.STALLED: ProgressPhase.STALLED,
}


def report_progress(event: ActivityProgressEvent) -> None:
    sink = current_progress_sink()
    if sink is None:
        return
    try:
        sink.emit(event)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"workflow progress sink failed: {exc}")


def emit_workflow_progress(
    graph,
    run_state,
    stage: str,
    status: WorkflowNodeStatus,
    detail: str | None = None,
) -> None:
    definition_id = graph._definition_id
    if not isinstance(definition_id, WorkflowDefinitionId):
        raise RuntimeError("workflow progress requires a canonical definition identity")
    report_progress(
        ActivityProgressEvent(
            ActivityProgressIdentity(run_state.activity_execution_id, definition_id),
            stage,
            _PHASES[status],
            detail,
        )
    )


def set_progress_sink(sink: ProgressEventSink | None) -> contextvars.Token:
    return bind_progress_sink(sink)


def _as_text(text: object) -> str:
    return str(text) if text is not None else ""


__all__ = [
    "ActivityRunStarted",
    "ActivityRunTerminal",
    "ProgressSink",
    "ProgressEventSink",
    "WorkflowEvent",
    "WorkflowCheckpointSink",
    "commit_workflow_checkpoint",
    "reset_checkpoint_sink",
    "emit_workflow_progress",
    "report_progress",
    "reset_progress_sink",
    "set_progress_sink",
    "set_checkpoint_sink",
]
