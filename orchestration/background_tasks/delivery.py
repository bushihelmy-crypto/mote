"""Progress reporting for bggraph tasks.

Projects typed Workflow progress facts into one BackgroundTask's output,
notification, and telemetry sinks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from mote.contracts.conversation import CauseBy
from mote.contracts.events.task import TaskProgressEvent
from mote.contracts.task.lifecycle import LocalTaskReference
from mote.contracts.task.progress import (
    ActivityProgressEvent,
    BackgroundTaskProgressEvent,
    ProgressEvent,
    ProgressEventSink,
)
from mote.orchestration.background_tasks.model import BackgroundTaskNotification
from mote.runtime.events.context import observe_event_sync
from mote.runtime.events.progress_scope import bind_progress_sink, reset_progress_sink
from mote.runtime.telemetry.logging import logger

END = "__end__"

set_progress_sink = bind_progress_sink


# ---------------------------------------------------------------------------
# Bggraph rendering helpers
# ---------------------------------------------------------------------------

_FMT_PROGRESS = "[{stage}] {status}: {detail}"

# The single authoritative definition of which *mid-flight* (per-node) progress
# events, emitted from inside the running graph coroutine, are worth
# interrupting the agent with — i.e. pushed into the msg_buffer to earn a new
# react turn (as opposed to merely appended to the task's disk output).
#
# Only a node **failure** qualifies: it is a decision point, so it wakes the agent
# immediately. Everything else the writer sees is disk-only:
#   * node ``success`` (``node_completed``) — progress, not a decision; the
#     final result is delivered once by the whole-task terminal (``_on_done``),
#   * the graph START marker — the tool's own return value already carries the
#     stage-summary, so no separate "task started" push is needed,
#   * mid-flight ``running`` updates.
# Every whole-task terminal (success / failed / timeout / cancelled) is pushed
# solely by ``pool._on_done`` (see :func:`_is_task_terminal`), not the writer.
_PUSH_WORTHY_STATUSES = frozenset({"failed"})


def _is_push_worthy(stage: str, status: str) -> bool:
    """Whether a *mid-flight* progress event should wake the agent.

    Only a per-node failure pushes (a decision point). Node success, the START
    marker and ``running`` updates are disk-only. Whole-task terminals and the
    whole-operation terminal events are pushed by ``pool._on_done``, not here.
    """

    return status in _PUSH_WORTHY_STATUSES


# Terminal statuses that, at the graph level (``stage == END``), mean the whole
# task has ended. Excludes the START marker (``stage == END`` with ``running``)
# and per-node terminals (``stage`` is a node name), which are mid-flight
# events rather than the one whole-task outcome.
_GRAPH_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled", "timeout"})


def _is_task_terminal(stage: str, status: str) -> bool:
    """Whether a push-worthy event is the *one* whole-task outcome.

    Used by the writer to *exclude* whole-task outcomes from delivery (the graph
    terminal is owned solely by ``pool._on_done``), so only per-node mid-flight
    decision events are eligible for delivery from inside the coroutine.
    """

    return stage == END and status in _GRAPH_TERMINAL_STATUSES


def _as_text(text: object) -> str:
    """Render *text* as a string. The full text is always returned so
    notifications/progress are never cut mid-content; a large result is
    persisted to disk by the shared tool-result exit, not truncated here.
    """
    return str(text) if text is not None else ""


def make_progress_sink(
    append: Callable[[str], None],
    *,
    reference: LocalTaskReference,
    command_name: str = "",
    deliver: Optional[Callable[[BackgroundTaskNotification], object]] = None,
) -> ProgressEventSink:
    """Build a typed sink that projects each event to task-owned sinks.

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
       react turn.
       Node *success* (``node_completed``) and the graph START marker are NOT
       delivered — they only land on disk; the final result reaches the agent
       once via the whole-task terminal. The *one* whole-task terminal (graph
       terminal) is likewise NOT delivered here — ``pool._on_done``
       is its sole producer (it also covers the interruption case where this
       coroutine is cancelled before its terminal code runs). The rich DAG
       snapshot the writer renders for that terminal still reaches the agent via
       the disk append (sink 1). The writer is agnostic to *how* delivery happens
       (no msg_buffer / wake here): the pool owns that and injects ``deliver``.

    **Broadcast to the world** (ambient telemetry, not injected):

    3. **telemetry** — mirrors *every* event as a
       :class:`TaskProgressEvent` onto the active telemetry context, captured at
       task-spawn time, so observation-only handlers
       (the REPL progress renderer, the log line) can watch live progress. This
       is fire-and-forget telemetry — same model as logging / ``bind_trace`` —
       and the contextvar scopes it to this task.
    """

    @dataclass(frozen=True, slots=True)
    class _BackgroundProgressSink:
        def emit(self, event: ProgressEvent) -> None:
            if not isinstance(event, ActivityProgressEvent):
                raise TypeError("background activity sink requires ActivityProgressEvent")
            stage = event.stage
            status_str = event.phase.value
            detail_str = event.detail or ""
            # The notify layer renders a ``(current)`` placeholder before the real
            # pool task_id is known. Substitute it here so every sink (disk append,
            # telemetry and the delivered notification) reports the real id.
            task_id = str(reference.task_id)
            if "(current)" in detail_str:
                detail_str = detail_str.replace("(current)", task_id)
            routed = BackgroundTaskProgressEvent(
                reference,
                event.stage,
                event.phase,
                detail_str or None,
            )
            line = _FMT_PROGRESS.format(stage=stage, status=status_str, detail=detail_str)
            append(line + "\n")
            _emit_task_progress(routed)
            # Deliver only a mid-flight node *failure* (the sole per-node decision
            # point ``_on_done`` cannot see). Node success and the START marker are
            # disk-only. The one whole-task terminal — a graph-level terminal or a
            # terminal — is NOT delivered here: ``pool._on_done`` is the single
            # terminal producer (it fires exactly once, and is the *only* producer
            # on an interruption where this coroutine is cancelled before reaching
            # its terminal code). The rich DAG snapshot the writer rendered for that
            # terminal still reaches the agent via the disk ``append`` above (the
            # task-attachment source of truth); only the redundant push is dropped.
            if deliver is not None and _is_push_worthy(stage, status_str) and not _is_task_terminal(stage, status_str):
                _deliver_progress(
                    deliver,
                    routed,
                    command_name,
                    detail_str,
                )

    return _BackgroundProgressSink()


def _deliver_progress(
    deliver: Callable[[BackgroundTaskNotification], object],
    event: BackgroundTaskProgressEvent,
    command_name: str,
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
        content=detail or f"[{event.stage}] {event.phase.value}",
        cause_by=CauseBy.RUN_COMMAND,
        task_id=event.reference.task_id,
        attempt_id=event.reference.attempt_id,
        command_name=command_name,
        status=event.phase.value,
        task_terminal=False,
    )
    try:
        deliver(notification)
    except Exception as exc:  # noqa: BLE001 — delivery must never break the pipeline
        logger.debug(f"bggraph: task notification delivery failed: {exc}")


def _emit_task_progress(event: BackgroundTaskProgressEvent) -> None:
    """Publish progress onto the active telemetry runtime (best-effort).

    This is a fire-and-forget observation through the ambient telemetry
    context. The runtime is bound explicitly
    inside the spawned task by ``BackgroundPool._with_progress`` (it captures the
    runtime synchronously at spawn time and re-binds it), so this
    does not depend on ``create_task`` snapshotting the contextvar across the
    spawn boundary. Losing it can only drop a progress mirror, never a control
    decision.

    ``report_progress`` is a synchronous API, so this uses the sync fan-out.
    No-ops without a ``task_id`` (the disk append is unaffected) or when no
    telemetry runtime is bound; failures never break the pipeline.
    """
    try:
        observe_event_sync(
            TaskProgressEvent(
                progress=event,
            )
        )
    except Exception as exc:  # noqa: BLE001 — emitting must never break the pipeline
        logger.debug(f"bggraph: task progress emit failed: {exc}")
