"""Stable nominal execution classification for tool definitions."""

from __future__ import annotations

from enum import Enum


class ToolExecutionKind(str, Enum):
    ATOMIC = "atomic"
    WORKFLOW_FOREGROUND = "workflow_foreground"
    WORKFLOW_DEFERRED = "workflow_deferred"

    @property
    def is_workflow(self) -> bool:
        return self is not ToolExecutionKind.ATOMIC


__all__ = ["ToolExecutionKind"]
