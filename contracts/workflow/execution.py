"""Typed result projected from the ToolExecutor for Workflow node dispatch."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.foundation.errors.report import ErrorReport
from mote.contracts.tool.result_payload import ToolPayload


@dataclass(frozen=True, slots=True)
class WorkflowNodeDispatchResult:
    output: str
    success: bool
    payload: ToolPayload | None = None
    error: ErrorReport | None = None

    def __post_init__(self) -> None:
        if type(self.output) is not str or type(self.success) is not bool:
            raise ValueError("Workflow node dispatch primitives are invalid")


__all__ = ["WorkflowNodeDispatchResult"]
