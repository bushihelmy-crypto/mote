"""Strict terminal facts owned by the durable Workflow domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from mote.contracts.artifact import ArtifactRef
from mote.contracts.workflow.identity import WorkflowRunId

MAX_WORKFLOW_INLINE_RESULT_BYTES = 64 * 1024
MAX_WORKFLOW_TERMINAL_TEXT_BYTES = 16 * 1024


def _validated_text(value: str, field: str, *, limit: int) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"Workflow terminal {field} must be non-empty text")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"Workflow terminal {field} exceeds its byte limit")


@dataclass(frozen=True, slots=True)
class WorkflowSucceededInline:
    content: str

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise TypeError("Workflow inline result must be text")
        if len(self.content.encode("utf-8")) > MAX_WORKFLOW_INLINE_RESULT_BYTES:
            raise ValueError("Workflow inline result exceeds its byte limit")


@dataclass(frozen=True, slots=True)
class WorkflowSucceededArtifact:
    artifact: ArtifactRef

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("Workflow artifact result requires ArtifactRef")


@dataclass(frozen=True, slots=True)
class WorkflowFailed:
    code: str
    message: str

    def __post_init__(self) -> None:
        _validated_text(self.code, "failure code", limit=256)
        _validated_text(
            self.message,
            "failure message",
            limit=MAX_WORKFLOW_TERMINAL_TEXT_BYTES,
        )


@dataclass(frozen=True, slots=True)
class WorkflowCancelled:
    reason: str

    def __post_init__(self) -> None:
        _validated_text(
            self.reason,
            "cancellation reason",
            limit=MAX_WORKFLOW_TERMINAL_TEXT_BYTES,
        )


@dataclass(frozen=True, slots=True)
class WorkflowTimedOut:
    reason: str

    def __post_init__(self) -> None:
        _validated_text(
            self.reason,
            "timeout reason",
            limit=MAX_WORKFLOW_TERMINAL_TEXT_BYTES,
        )


WorkflowTerminalOutcome: TypeAlias = (
    WorkflowSucceededInline | WorkflowSucceededArtifact | WorkflowFailed | WorkflowCancelled | WorkflowTimedOut
)


@dataclass(frozen=True, slots=True)
class WorkflowTerminalResult:
    run_id: WorkflowRunId
    terminal_revision: int
    outcome: WorkflowTerminalOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, WorkflowRunId):
            raise TypeError("workflow terminal result requires WorkflowRunId")
        if type(self.terminal_revision) is not int or self.terminal_revision < 1:
            raise ValueError("workflow terminal revision must be positive")


__all__ = [
    "MAX_WORKFLOW_INLINE_RESULT_BYTES",
    "MAX_WORKFLOW_TERMINAL_TEXT_BYTES",
    "WorkflowCancelled",
    "WorkflowFailed",
    "WorkflowSucceededArtifact",
    "WorkflowSucceededInline",
    "WorkflowTerminalOutcome",
    "WorkflowTerminalResult",
    "WorkflowTimedOut",
]
