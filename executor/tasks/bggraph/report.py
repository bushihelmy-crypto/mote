"""Progress reporting for bggraph tasks.

Provides both the generic progress ContextVar primitive (``report_progress``,
``set_progress_writer``, ``reset_progress_writer``) and the bggraph-specific
rendering helpers (``make_progress_writer``, ``_truncate``).
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Generic progress reporting via contextvars
# ---------------------------------------------------------------------------

# Writer signature: (stage: str, status: Any, detail: Any) -> None
# ``status`` is Any so that both ``BgStatus`` and plain strings work without
# a hard dependency on the enum definition.
ProgressWriter = Callable[[str, Any, Any], None]

_progress_ctx: contextvars.ContextVar[Optional[ProgressWriter]] = contextvars.ContextVar(
    "_progress_ctx", default=None
)


def report_progress(stage: str, status: Any, detail: Any = None) -> None:
    """Report a progress event. No-op outside a background task context."""
    writer = _progress_ctx.get()
    if writer is not None:
        try:
            writer(stage, status, detail)
        except Exception:  # best-effort — never break the pipeline on a sink error
            pass


def set_progress_writer(writer: Optional[ProgressWriter]) -> contextvars.Token:
    """Install *writer* as the active progress sink; returns the reset token."""
    return _progress_ctx.set(writer)


def reset_progress_writer(token: contextvars.Token) -> None:
    """Restore the previous progress sink."""
    _progress_ctx.reset(token)


# ---------------------------------------------------------------------------
# Bggraph rendering helpers
# ---------------------------------------------------------------------------

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


def make_progress_writer(append: Callable[[str], None], *, task_id: str = "") -> ProgressWriter:
    """Build a writer that renders each event and appends it via *append*.

    *append* is typically ``lambda line: store.append(task_id, line)``. The disk
    append stays the source of truth; in addition, when ``task_id`` is given,
    each event is mirrored onto the active event bus as a
    :class:`TaskProgressEvent` so subscribers can observe live progress.
    """

    def _writer(stage: str, status: Any, detail: Any = None) -> None:
        status_str = status.value if hasattr(status, "value") else str(status)
        detail_str = _truncate(detail) if detail is not None else ""
        line = _FMT_PROGRESS.format(stage=stage, status=status_str, detail=detail_str)
        append(line + "\n")
        _emit_task_progress(task_id, stage, status_str, detail_str)

    return _writer


def _emit_task_progress(task_id: str, stage: str, status: str, detail: str) -> None:
    """Mirror a progress event onto the active bus (best-effort, sync emit).

    ``report_progress`` is a synchronous API, so this uses the sync fan-out.
    No-ops without a ``task_id`` (the disk append is unaffected) or when no bus
    is bound; swallows any failure so emitting never breaks the pipeline.
    """
    if not task_id:
        return
    try:
        from metagpt.common.events import TaskProgressEvent, emit_event_sync

        emit_event_sync(
            TaskProgressEvent(task_id=task_id, stage=stage, status=status, detail=detail)
        )
    except Exception:  # noqa: BLE001 — emitting must never break the pipeline
        pass
