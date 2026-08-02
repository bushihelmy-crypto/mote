"""Typed progress facts shared across Workflow and BackgroundTask boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias

from mote.contracts.task.lifecycle import LocalTaskReference
from mote.contracts.workflow.identity import WorkflowRunReference


class ProgressPhase(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    WAITING_FOR_ROUTE = "waiting_for_route"
    STALLED = "stalled"


@dataclass(frozen=True, slots=True)
class ActivityProgressIdentity:
    execution_id: str
    definition_id: str

    def __post_init__(self) -> None:
        if not self.execution_id or not self.definition_id:
            raise ValueError("activity progress identity fields must not be empty")


@dataclass(frozen=True, slots=True)
class ActivityProgressEvent:
    identity: ActivityProgressIdentity
    stage: str
    phase: ProgressPhase
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.stage:
            raise ValueError("workflow progress stage must not be empty")


@dataclass(frozen=True, slots=True)
class DurableWorkflowRunProgress:
    reference: WorkflowRunReference
    observed_run_revision: int
    node_id: str
    phase: ProgressPhase
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.observed_run_revision) is not int or self.observed_run_revision < 1:
            raise ValueError("observed Workflow revision must be positive")
        if not self.node_id:
            raise ValueError("durable Workflow progress node must not be empty")


@dataclass(frozen=True, slots=True)
class BackgroundTaskProgressEvent:
    reference: LocalTaskReference
    stage: str
    phase: ProgressPhase
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.stage:
            raise ValueError("background-task progress stage must not be empty")


ProgressEvent: TypeAlias = ActivityProgressEvent | DurableWorkflowRunProgress | BackgroundTaskProgressEvent


class ProgressEventSink(Protocol):
    def emit(self, event: ProgressEvent) -> None: ...


__all__ = [
    "BackgroundTaskProgressEvent",
    "ActivityProgressEvent",
    "ActivityProgressIdentity",
    "DurableWorkflowRunProgress",
    "ProgressEvent",
    "ProgressEventSink",
    "ProgressPhase",
]
