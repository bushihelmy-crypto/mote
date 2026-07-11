"""LogSubscriber — semantic event logging on the spine.

A pure-observation subscriber that emits one concise log line per agent
lifecycle event (session / turn / message / tool / compaction / ...), giving an
*event-level* trace that complements the *method-level* ``@log_class`` decorator.
The two sit at different granularities: ``@log_class`` traces every public method
call; this names the handful of curated domain events. It is not a replacement.

It lives next to the bus because it depends only on ``common.logs`` (the logger)
and the sibling event types — adding no new layering edge (``common.events``
already imports ``common.logs``).

LLM stream deltas are deliberately *not* logged: they arrive via the bus's sync
fan-out (``handle_sync``), which this subscriber does not implement, so per-token
chunks never reach :meth:`handle` (and would flood the log if they did).

Best-effort: as an observation subscriber :meth:`handle` returns nothing (the bus
structurally drops an observer's return — it can never fold an outcome) and
swallows its own errors — a logging failure must never break a turn.
"""

from __future__ import annotations

from typing import Any, Callable

from mote.common.events.types import (
    AgentLifecycleEvent,
    CompactionCheckpointEvent,
    DiagnosticsEvent,
    FileChangedEvent,
    FileSnapshotEvent,
    MessageAppendedEvent,
    PostCompactEvent,
    PostToolUseEvent,
    PreCompactEvent,
    PreToolUseEvent,
    RecoveryEvent,
    ResourceReportEvent,
    SessionEndEvent,
    SessionStartEvent,
    TaskProgressEvent,
    TurnEndEvent,
    TurnStartEvent,
    UserPromptSubmitEvent,
)
from mote.common.interface.event_subscriber import ObservationSubscriber, ObserverPriority, SyncObserver
from mote.common.logs import logger
from mote.common.text import collapse_whitespace


def _clip(text: str) -> str:
    return collapse_whitespace(str(text))


def _fmt_message_appended(e) -> str:
    msg = e.message
    role = getattr(msg, "role", "?")
    content = getattr(msg, "content", "") or ""
    return f"event message_appended role={role} chars={len(content)} '{_clip(content)}'"


def _fmt_agent_lifecycle(e) -> str:
    detail = f" {e.detail}" if e.detail else ""
    return f"event agent_lifecycle phase={e.phase} id={e.session_id[:8] or '?'}{detail}"


# Per-event-type log rendering: maps an event class to ``(level, format_fn)``.
# Milestone events log at INFO; routine per-turn events at DEBUG. Dispatch is by
# exact type (the logged events form a flat hierarchy — none subclasses another),
# so a table lookup replaces a long isinstance/elif chain; an unlisted type falls
# back to a bare name line. Kept module-level (built once) rather than rebuilt per
# call.
_EVENT_LOG: dict[type, tuple[str, "Callable[[Any], str]"]] = {
    SessionStartEvent: (
        "info",
        lambda e: f"event session_start id={e.session_id[:8] or '?'} source={e.source} model={e.model or '?'}",
    ),
    SessionEndEvent: ("info", lambda e: f"event session_end id={e.session_id[:8] or '?'}"),
    CompactionCheckpointEvent: (
        "info",
        lambda e: f"event compaction_checkpoint messages={len(e.messages)} summary='{_clip(e.summary)}'",
    ),
    PreCompactEvent: ("info", lambda e: f"event pre_compact trigger={e.trigger}"),
    PostCompactEvent: ("info", lambda e: f"event post_compact trigger={e.trigger}"),
    TurnStartEvent: ("debug", lambda e: f"event turn_start turn={e.turn_id or '?'}"),
    TurnEndEvent: ("debug", lambda e: f"event turn_end turn={e.turn_id or '?'} model={e.model or '?'}"),
    MessageAppendedEvent: ("debug", _fmt_message_appended),
    UserPromptSubmitEvent: ("debug", lambda e: f"event user_prompt_submit '{_clip(e.prompt)}'"),
    PreToolUseEvent: ("debug", lambda e: f"event pre_tool_use tool={e.tool_name}"),
    PostToolUseEvent: ("debug", lambda e: f"event post_tool_use tool={e.tool_name}"),
    FileSnapshotEvent: (
        "debug",
        lambda e: f"event file_snapshot op={e.operation} backend={e.backend} path={e.display_path or e.path}",
    ),
    FileChangedEvent: ("debug", lambda e: f"event file_changed type={e.change_type} path={e.path}"),
    DiagnosticsEvent: ("debug", lambda e: f"event diagnostics files={len(e.paths)} chars={len(e.block)}"),
    AgentLifecycleEvent: ("info", _fmt_agent_lifecycle),
    RecoveryEvent: (
        "info",
        lambda e: (
            f"event recovery phase={e.phase} action={e.action} "
            f"attempt={e.attempt} error={e.error_type}: '{e.error}'"
        ),
    ),
    ResourceReportEvent: (
        "debug",
        lambda e: f"event resource_report block={e.block} name={e.name_} role={e.role or '?'}",
    ),
}


class LogSubscriber(ObservationSubscriber, SyncObserver):
    """Logs each semantic bus event as one concise line (observation-only)."""

    #: Late (after recorder at PERSIST) — purely cosmetic since it folds nothing,
    #: but it reads cleanly as "log what finally happened".
    priority: int = ObserverPriority.LOG

    async def handle(self, event) -> None:
        # TaskProgress and agent-lifecycle ride the sync fan-out (handle_sync);
        # ignore them here so they aren't logged twice should one ever reach the
        # async path.
        if isinstance(event, (TaskProgressEvent, AgentLifecycleEvent)):
            return
        try:
            self._log(event)
        except Exception as exc:  # noqa: BLE001 — logging must never break a turn
            logger.warning(f"LogSubscriber: failed to log {getattr(event, 'name', '?')}: {exc}")

    def handle_sync(self, event) -> None:
        # Narrowly opt into the sync fan-out: low-frequency background task
        # progress and the orchestration layer's agent-lifecycle milestones
        # (both emitted from sync call sites). Per-token stream deltas are
        # deliberately *not* logged.
        try:
            if isinstance(event, TaskProgressEvent):
                logger.debug(
                    f"event task_progress task={event.task_id} stage={event.stage} "
                    f"status={event.status} '{_clip(event.detail)}'"
                )
            elif isinstance(event, AgentLifecycleEvent):
                self._log(event)
        except Exception as exc:  # noqa: BLE001 — logging must never break a turn
            logger.warning(f"LogSubscriber: failed to log {getattr(event, 'name', '?')}: {exc}")

    @staticmethod
    def _log(event) -> None:
        entry = _EVENT_LOG.get(type(event))
        if entry is None:
            logger.debug(f"event {getattr(event, 'name', '?')}")
            return
        level, fmt = entry
        getattr(logger, level)(fmt(event))


__all__ = ["LogSubscriber"]
