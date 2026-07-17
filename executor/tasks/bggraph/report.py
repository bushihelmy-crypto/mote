"""Progress reporting for bggraph tasks.

Provides both the generic progress ContextVar primitive (``report_progress``,
``set_progress_writer``, ``reset_progress_writer``) and the bggraph-specific
rendering helpers (``make_progress_writer``, ``_as_text``).
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, Optional

from mote.common.events import TaskProgressEvent, observe_event_sync
from mote.common.logs import logger
from mote.common.schema import CauseBy
from mote.executor.tasks.bggraph.types import END
from mote.executor.tasks.types import BackgroundTaskNotification

# ---------------------------------------------------------------------------
# Generic progress reporting via contextvars
# ---------------------------------------------------------------------------

# Writer signature: (stage: str, status: Any, detail: Any) -> None
# ``status`` is Any so that both ``BgStatus`` and plain strings work without
# a hard dependency on the enum definition.
ProgressWriter = Callable[[str, Any, Any], None]

_progress_ctx: contextvars.ContextVar[Optional[ProgressWriter]] = contextvars.ContextVar("_progress_ctx", default=None)


def report_progress(stage: str, status: Any, detail: Any = None) -> None:
    """Report a progress event. No-op outside a background task context."""
    writer = _progress_ctx.get()
    if writer is not None:
        try:
            writer(stage, status, detail)
        except Exception as exc:  # noqa: BLE001 — never break the pipeline on a sink error
            logger.debug(f"report_progress: progress writer failed: {exc}")


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

# The single authoritative definition of which *mid-flight* (per-node) progress
# events, emitted from inside the running graph coroutine, are worth
# interrupting the agent with — i.e. pushed into the msg_buffer to earn a new
# react turn (as opposed to merely appended to the task's disk output).
#
# Only a node **failure** qualifies: it is a decision point (the model must
# GetNodeState / resume_tasks or ask the user), so it wakes the agent
# immediately. Everything else the writer sees is disk-only:
#   * node ``success`` (``node_completed``) — progress, not a decision; the
#     final result is delivered once by the whole-task terminal (``_on_done``),
#   * the graph START marker — the tool's own return value already carries the
#     stage-summary, so no separate "task started" push is needed,
#   * mid-flight ``running`` updates.
# The other decision point — an LLM-route pause (``waiting_for_route``) — and
# every whole-task terminal (success / failed / timeout / cancelled) are pushed
# solely by ``pool._on_done`` (see :func:`_is_task_terminal`), not the writer.
_PUSH_WORTHY_STATUSES = frozenset({"failed"})


def _is_push_worthy(stage: str, status: str) -> bool:
    """Whether a *mid-flight* progress event should wake the agent.

    Only a per-node failure pushes (a decision point). Node success, the START
    marker and ``running`` updates are disk-only. Whole-task terminals and the
    route pause are pushed by ``pool._on_done``, not from here (the writer's
    ``_is_task_terminal`` gate excludes them regardless).
    """

    return status in _PUSH_WORTHY_STATUSES


# Terminal statuses that, at the graph level (``stage == END``), mean the whole
# task has ended. A pause (``waiting_for_route`` / ``stalled``) is a whole-task
# outcome too (the driver returns a ``GraphPause``) regardless of stage — see
# ``_GRAPH_PAUSE_STATUSES`` below. Excludes the START marker (``stage == END``
# with ``running``) and per-node terminals (``stage`` is a node name), which are
# mid-flight events — not the one whole-task outcome.
_GRAPH_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled", "timeout"})


# Whole-task pause statuses — a pause is not terminal (the task keeps its
# snapshot for resume) but IS a whole-task outcome owned by ``pool._on_done``,
# so the writer must not also deliver it. Two reasons share this: an LLM-route
# pause (``waiting_for_route``) and a deadlocked-join stall (``stalled``).
_GRAPH_PAUSE_STATUSES = frozenset({"waiting_for_route", "stalled"})


def _is_task_terminal(stage: str, status: str) -> bool:
    """Whether a push-worthy event is the *one* whole-task outcome.

    Used by the writer to *exclude* whole-task outcomes from delivery (the graph
    terminal and every pause are owned solely by ``pool._on_done``), so only the
    START marker and per-node mid-flight events are pushed from inside the
    coroutine. Distinguishes the graph-level terminal (``stage == END`` with a
    terminal status) and a pause (``waiting_for_route`` / ``stalled``) from
    mid-flight node events. (Despite the name, "terminal" here means "the one
    whole-task outcome push" — a pause is one such outcome even though the task
    is resumable, not ended.)
    """

    if status in _GRAPH_PAUSE_STATUSES:
        return True
    return stage == END and status in _GRAPH_TERMINAL_STATUSES


def _as_text(text: Any) -> str:
    """Render *text* as a string. The full text is always returned so
    notifications/progress are never cut mid-content; a large result is
    persisted to disk by the shared tool-result exit, not truncated here.
    """
    return str(text) if text is not None else ""


def make_progress_writer(
    append: Callable[[str], None],
    *,
    task_id: str = "",
    command_name: str = "",
    deliver: Optional[Callable[[Any], None]] = None,
) -> ProgressWriter:
    """Build a writer that renders each event and routes it to its sinks.

    These sinks are *not* a symmetric trio — they split along two distinct
    intents:

    **Routed to this task's owner** (injected dependencies, threaded in by the
    pool that owns the resources):

    1. **disk** (always) — *append* renders every line to the task's output, the
       source of truth behind ``<task-attachment>`` blocks. Typically
       ``lambda line: store.append(task_id, line)``.
    2. **deliver** (when *deliver* is given) — for a *mid-flight node failure*
       only (see :func:`_is_push_worthy` minus :func:`_is_task_terminal`) builds
       a structured :class:`BackgroundTaskNotification` and hands it to *deliver*
       (the pool's single push+wake choke point), so a failed node earns a new
       react turn (a decision point: GetNodeState / resume_tasks / ask the user).
       Node *success* (``node_completed``) and the graph START marker are NOT
       delivered — they only land on disk; the final result reaches the agent
       once via the whole-task terminal. The *one* whole-task terminal (graph
       terminal / route pause) is likewise NOT delivered here — ``pool._on_done``
       is its sole producer (it also covers the interruption case where this
       coroutine is cancelled before its terminal code runs). The rich DAG
       snapshot the writer renders for that terminal still reaches the agent via
       the disk append (sink 1). The writer is agnostic to *how* delivery happens
       (no msg_buffer / wake here): the pool owns that and injects ``deliver``.

    **Broadcast to the world** (ambient telemetry, not injected):

    3. **event bus** (when ``task_id`` is given) — mirrors *every* event as a
       :class:`TaskProgressEvent` onto the active bus (the ``_ACTIVE_BUS``
       contextvar, captured at task-spawn time) so observation-only subscribers
       (the REPL progress renderer, the log line) can watch live progress. This
       is fire-and-forget telemetry — same model as logging / ``bind_trace`` —
       so it deliberately reaches the ambient bus rather than being injected:
       the contextvar already scopes the bus to this task, so threading a bus
       reference through would only re-implement that by hand.
    """

    def _writer(stage: str, status: Any, detail: Any = None) -> None:
        status_str = status.value if hasattr(status, "value") else str(status)
        detail_str = _as_text(detail) if detail is not None else ""
        # The notify layer renders a ``(current)`` placeholder before the real
        # pool task_id is known. Substitute it here so every sink (disk append,
        # event bus and the delivered notification) reports the real id.
        if task_id and "(current)" in detail_str:
            detail_str = detail_str.replace("(current)", task_id)
        line = _FMT_PROGRESS.format(stage=stage, status=status_str, detail=detail_str)
        append(line + "\n")
        _emit_task_progress(task_id, stage, status_str, detail_str)
        # Deliver only a mid-flight node *failure* (the sole per-node decision
        # point ``_on_done`` cannot see). Node success and the START marker are
        # disk-only. The one whole-task terminal — a graph-level terminal or a
        # route pause — is NOT delivered here: ``pool._on_done`` is the single
        # terminal producer (it fires exactly once, and is the *only* producer
        # on an interruption where this coroutine is cancelled before reaching
        # its terminal code). The rich DAG snapshot the writer rendered for that
        # terminal still reaches the agent via the disk ``append`` above (the
        # task-attachment source of truth); only the redundant push is dropped.
        if deliver is not None and _is_push_worthy(stage, status_str) and not _is_task_terminal(stage, status_str):
            _deliver_progress(deliver, task_id, command_name, stage, status_str, detail_str)

    return _writer


def _deliver_progress(
    deliver: Callable[[Any], None],
    task_id: str,
    command_name: str,
    stage: str,
    status: str,
    detail: str,
) -> None:
    """Build a structured notification for a non-terminal progress event and
    hand it to *deliver*.

    Only the START marker and per-node mid-flight events flow through here (the
    writer's terminal gate excludes whole-task terminals — see ``_writer``), so
    these are never flagged ``task_terminal``: the single whole-task terminal is
    owned exclusively by ``pool._on_done``. Best-effort: a delivery failure must
    never break the task pipeline. The ``detail`` is already
    ``(current)``-substituted by the writer.
    """

    notification = BackgroundTaskNotification(
        content=detail or f"[{stage}] {status}",
        cause_by=CauseBy.RUN_COMMAND,
        task_id=task_id,
        command_name=command_name,
        status=status,
        task_terminal=False,
    )
    try:
        deliver(notification)
    except Exception as exc:  # noqa: BLE001 — delivery must never break the pipeline
        logger.debug(f"bggraph: task notification delivery failed: {exc}")


def _emit_task_progress(task_id: str, stage: str, status: str, detail: str) -> None:
    """Broadcast a progress event onto the active bus (best-effort, sync observe).

    This is the *telemetry* sink: a fire-and-forget **observation** onto the
    ambient ``_ACTIVE_BUS`` contextvar. That contextvar is bound explicitly
    inside the spawned task by ``BackgroundPool._with_progress`` (it captures the
    bus synchronously at spawn time and re-binds it with ``set_bus``), so this
    does not depend on ``create_task`` snapshotting the contextvar across the
    spawn boundary. Being observation-only, a lost bus could only drop a progress
    mirror, never a control veto.

    ``report_progress`` is a synchronous API, so this uses the sync fan-out.
    No-ops without a ``task_id`` (the disk append is unaffected) or when no bus
    is bound; swallows any failure so emitting never breaks the pipeline.
    """
    if not task_id:
        return
    try:
        observe_event_sync(TaskProgressEvent(task_id=task_id, stage=stage, status=status, detail=detail))
    except Exception as exc:  # noqa: BLE001 — emitting must never break the pipeline
        logger.debug(f"bggraph: task progress emit failed: {exc}")
