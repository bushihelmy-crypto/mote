"""Stable contracts for background task identity and outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType, TypeAlias

SessionId = NewType("SessionId", str)
TaskId = NewType("TaskId", str)
CommandName = NewType("CommandName", str)


@dataclass(frozen=True, slots=True)
class TaskResultRecord:
    task_id: TaskId
    content: str


@dataclass(frozen=True, slots=True)
class InlineTaskOutput:
    content: str


@dataclass(frozen=True, slots=True)
class StoredTaskOutput:
    locator: str

    def __post_init__(self) -> None:
        if not self.locator.startswith("task-output:"):
            raise ValueError("stored task output requires an opaque task-output locator")


@dataclass(frozen=True, slots=True)
class TaskFailure:
    message: str


@dataclass(frozen=True, slots=True)
class PauseReason:
    message: str


@dataclass(frozen=True, slots=True)
class CompletedInlineTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    output: InlineTaskOutput


@dataclass(frozen=True, slots=True)
class CompletedStoredTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    output: StoredTaskOutput


@dataclass(frozen=True, slots=True)
class FailedTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    error: TaskFailure


@dataclass(frozen=True, slots=True)
class PausedTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    reason: PauseReason


TaskResultPointer: TypeAlias = (
    CompletedInlineTaskResultPointer
    | CompletedStoredTaskResultPointer
    | FailedTaskResultPointer
    | PausedTaskResultPointer
)


__all__ = [
    "CommandName",
    "CompletedInlineTaskResultPointer",
    "CompletedStoredTaskResultPointer",
    "FailedTaskResultPointer",
    "InlineTaskOutput",
    "PausedTaskResultPointer",
    "PauseReason",
    "SessionId",
    "StoredTaskOutput",
    "TaskFailure",
    "TaskId",
    "TaskResultPointer",
    "TaskResultRecord",
]
