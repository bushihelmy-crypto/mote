"""Canonical nominal identity for durable Workflow runs."""

from __future__ import annotations

from dataclasses import dataclass


class WorkflowRunId(str):
    def __new__(cls, value: str) -> "WorkflowRunId":
        if type(value) is not str or not value:
            raise ValueError("WorkflowRunId must be a non-empty string")
        return str.__new__(cls, value)


class WorkflowDefinitionId(str):
    def __new__(cls, value: str) -> "WorkflowDefinitionId":
        if type(value) is not str or not value:
            raise ValueError("WorkflowDefinitionId must be a non-empty string")
        return str.__new__(cls, value)


@dataclass(frozen=True, slots=True, order=True)
class WorkflowRunReference:
    run_id: WorkflowRunId
    definition_id: WorkflowDefinitionId

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, WorkflowRunId):
            raise TypeError("workflow reference requires WorkflowRunId")
        if not isinstance(self.definition_id, WorkflowDefinitionId):
            raise TypeError("workflow reference requires WorkflowDefinitionId")


__all__ = ["WorkflowDefinitionId", "WorkflowRunId", "WorkflowRunReference"]
