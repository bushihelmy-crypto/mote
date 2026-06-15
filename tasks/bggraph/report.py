"""Progress reporting for bggraph nodes (ported from design v5 §8).

A ``contextvars.ContextVar`` carries a per-task *writer* callable. Node code (and
the engine) call :func:`report_progress` without threading a sink through every
call; outside a background context the call is a harmless no-op.
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, Optional

from metagpt.tasks.bggraph.types import NodeStatus

# Writer signature: (stage: str, status: NodeStatus, detail: Any) -> None
_ProgressWriter = Callable[[str, NodeStatus, Any], None]

_progress_ctx: contextvars.ContextVar[Optional[_ProgressWriter]] = contextvars.ContextVar(
    "_bggraph_progress_ctx", default=None
)

# Rendered line appended to the task's disk output for each progress event.
_FMT_PROGRESS = "[{stage}] {status}: {detail}"

MAX_RESULT_DISPLAY_CHARS = 500


def _truncate(text: Any, limit: int = MAX_RESULT_DISPLAY_CHARS) -> str:
    """Truncate *text*, keeping head and tail with a middle ellipsis."""
    s = str(text) if text is not None else ""
    if len(s) <= limit:
        return s
    half = limit // 2
    omitted = len(s) - limit
    return s[:half] + f"\n[... TRUNCATED {omitted} CHARS ...]\n" + s[-half:]


def report_progress(stage: str, status: NodeStatus, detail: Any = None) -> None:
    """Report a progress event. No-op outside a background task context."""
    writer = _progress_ctx.get()
    if writer is not None:
        try:
            writer(stage, status, detail)
        except Exception:  # best-effort — never break the pipeline on a sink error
            pass


def make_progress_writer(append: Callable[[str], None]) -> _ProgressWriter:
    """Build a writer that renders each event and appends it via *append*.

    *append* is typically ``lambda line: store.append(task_id, line)``.
    """

    def _writer(stage: str, status: NodeStatus, detail: Any = None) -> None:
        status_str = status.value if isinstance(status, NodeStatus) else str(status)
        line = _FMT_PROGRESS.format(
            stage=stage,
            status=status_str,
            detail=_truncate(detail) if detail is not None else "",
        )
        append(line + "\n")

    return _writer


def set_progress_writer(writer: Optional[_ProgressWriter]) -> contextvars.Token:
    """Install *writer* as the active progress sink; returns the reset token."""
    return _progress_ctx.set(writer)


def reset_progress_writer(token: contextvars.Token) -> None:
    """Restore the previous progress sink."""
    _progress_ctx.reset(token)
