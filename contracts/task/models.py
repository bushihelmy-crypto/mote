"""Stable contracts for background task identity and outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType, TypeAlias

from mote.contracts.artifact import ArtifactRef

TaskId = NewType("TaskId", str)
CommandName = NewType("CommandName", str)


@dataclass(frozen=True, slots=True, order=True)
class AttemptId:
    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or self.value < 1:
            raise ValueError("AttemptId must be a positive integer")


@dataclass(frozen=True, slots=True)
class TaskResultRecord:
    task_id: TaskId
    content: str


@dataclass(frozen=True, slots=True)
class InlineTaskOutput:
    content: str


@dataclass(frozen=True, slots=True)
class TaskFailure:
    message: str


@dataclass(frozen=True, slots=True)
class CompletedInlineTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    output: InlineTaskOutput


@dataclass(frozen=True, slots=True)
class CompletedArtifactTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    output: ArtifactRef


@dataclass(frozen=True, slots=True)
class FailedTaskResultPointer:
    task_id: TaskId
    command_name: CommandName
    summary: str
    error: TaskFailure


TaskResultPointer: TypeAlias = (
    CompletedInlineTaskResultPointer | CompletedArtifactTaskResultPointer | FailedTaskResultPointer
)


__all__ = [
    "CommandName",
    "AttemptId",
    "CompletedInlineTaskResultPointer",
    "CompletedArtifactTaskResultPointer",
    "FailedTaskResultPointer",
    "InlineTaskOutput",
    "TaskFailure",
    "TaskId",
    "TaskResultPointer",
    "TaskResultRecord",
]
