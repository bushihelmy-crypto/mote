"""Pipeline detection — mark a compiled-graph executor and recognise its tool.

A *pipeline tool* is a BaseTool whose work is driven by a ``BgGraph`` compiled
into an async executor (``BgGraph.compile()`` → ``_build_executor``). Rather than
each pipeline tool declaring a flag, the executor function returned by
``compile()`` is stamped with :data:`PIPELINE_EXECUTOR_MARKER`; a tool counts as
a pipeline tool when any of its instance attributes holds such a stamped
executor. This keeps categorisation automatic — wiring a compiled graph into a
tool is what makes it a pipeline, with no separate decorator to keep in sync.
"""
from __future__ import annotations

from typing import Any, Callable

# Attribute stamped onto the async executor returned by BgGraph.compile().
PIPELINE_EXECUTOR_MARKER = "_is_bg_pipeline_executor"


def mark_pipeline_executor(fn: Callable) -> Callable:
    """Stamp a compiled-graph executor so its owning tool is recognisable."""
    setattr(fn, PIPELINE_EXECUTOR_MARKER, True)
    return fn


def is_pipeline_tool(tool: Any) -> bool:
    """Return True if *tool* is backed by a compiled BgGraph executor.

    Scans the instance attributes for a value carrying
    :data:`PIPELINE_EXECUTOR_MARKER` (the stamp ``mark_pipeline_executor`` adds
    to a ``compile()`` result). A non-pipeline tool — built-in or MCP adapter —
    holds no such value and returns False.
    """
    try:
        values = vars(tool).values()
    except TypeError:
        return False
    return any(getattr(v, PIPELINE_EXECUTOR_MARKER, False) for v in values)
