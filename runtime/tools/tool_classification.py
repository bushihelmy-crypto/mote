"""Runtime classification of bound tool instances without orchestration imports."""

from __future__ import annotations

from typing import Any

_PIPELINE_EXECUTOR_MARKER = "_is_bg_pipeline_executor"


def is_pipeline_tool(tool: Any) -> bool:
    """Return whether a tool owns an orchestration-compiled executor.

    The marker is part of the callable's structural contract.  Runtime reads it
    without importing the Orchestration implementation that stamps it.
    Explicit graph tools also declare ``is_graph_tool`` before their compiled
    executor is materialized.
    """

    tool = getattr(tool, "wrapped_tool", tool)
    if bool(getattr(tool, "is_graph_tool", False)):
        return True
    try:
        values = vars(tool).values()
    except TypeError:
        return False
    return any(bool(getattr(value, _PIPELINE_EXECUTOR_MARKER, False)) for value in values)


__all__ = ["is_pipeline_tool"]
