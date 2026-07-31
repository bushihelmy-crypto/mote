"""Runtime classification from immutable tool definitions."""

from __future__ import annotations

from typing import Any

from mote.contracts.tool.execution import ToolExecutionKind


def is_pipeline_tool(tool: Any) -> bool:
    """Return whether a tool owns an orchestration-compiled executor.

    Runtime reads only the frozen definition and never probes capability state.
    """

    definition = getattr(tool, "definition", None)
    return getattr(definition, "execution_kind", ToolExecutionKind.ATOMIC).is_workflow


__all__ = ["is_pipeline_tool"]
