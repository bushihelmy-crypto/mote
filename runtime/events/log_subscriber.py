"""LogSubscriber — semantic event logging from Telemetry.

A pure-observation subscriber that emits one concise log line per agent
lifecycle event (session / turn / message / tool / compaction / ...), giving an
*event-level* trace that complements the *method-level* ``@log_class`` decorator.
The two sit at different granularities: ``@log_class`` traces every public method
call; this names the handful of curated domain events. It is not a replacement.

It lives next to Telemetry because it depends only on runtime logging and sibling
event types, adding no new layering edge.

LLM stream deltas are deliberately *not* logged: they arrive via Telemetry's sync
fan-out (``handle_sync``), which this subscriber does not implement, so per-token
chunks never reach :meth:`handle` (and would flood the log if they did).

Best-effort: as a telemetry handler :meth:`handle` returns nothing (Telemetry
structurally drops an observer's return — it can never fold an outcome) and
swallows its own errors — a logging failure must never break a turn.
"""

from __future__ import annotations

from typing import Any, Callable

from mote.contracts.events.agent import AgentLifecycleEvent
from mote.contracts.events.conversation import (
    ContextCompactedEvent,
    MessageAppendedEvent,
    PostCompactEvent,
    PromptRejectedEvent,
    UserPromptSubmitEvent,
)
from mote.contracts.events.file.observation import FileChangedEvent
from mote.contracts.events.session import SessionEndEvent, SessionStartEvent, TurnEndEvent, TurnStartEvent
from mote.contracts.events.task import TaskProgressEvent
from mote.contracts.events.telemetry import DiagnosticsEvent, RecoveryEvent, ResourceReportEvent
from mote.contracts.events.tool import ToolCallFinishedEvent, ToolInvocationStartedEvent
from mote.runtime.telemetry.logging import logger
from mote.runtime.tools.text_normalization import collapse_whitespace


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
    SessionEndEvent: (
        "info",
        lambda e: f"event session_end id={e.session_id[:8] or '?'}",
    ),
    ContextCompactedEvent: (
        "info",
        lambda e: f"event context_compacted messages={len(e.model_context_messages)} summary='{_clip(e.summary)}'",
    ),
    PostCompactEvent: ("info", lambda e: f"event post_compact trigger={e.trigger}"),
    TurnStartEvent: ("debug", lambda e: f"event turn_start turn={e.turn_id or '?'}"),
    TurnEndEvent: (
        "debug",
        lambda e: f"event turn_end turn={e.turn_id or '?'} model={e.model or '?'}",
    ),
    MessageAppendedEvent: ("debug", _fmt_message_appended),
    UserPromptSubmitEvent: (
        "debug",
        lambda e: f"event user_prompt_submit '{_clip(e.prompt)}'",
    ),
    PromptRejectedEvent: (
        "info",
        lambda e: f"event prompt_rejected terminate={e.terminate} reason='{_clip(e.reason)}'",
    ),
    ToolInvocationStartedEvent: (
        "debug",
        lambda e: f"event tool_invocation_started tool={e.tool_name}",
    ),
    ToolCallFinishedEvent: (
        "debug",
        lambda e: f"event tool_call_finished tool={e.tool_name} outcome={e.outcome}",
    ),
    FileChangedEvent: (
        "debug",
        lambda e: (f"event file_changed type={e.change_type} " f"attribution={e.attribution} path={e.path}"),
    ),
    DiagnosticsEvent: (
        "debug",
        lambda e: f"event diagnostics files={len(e.paths)} chars={len(e.block)}",
    ),
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


class LogSubscriber:
    """Logs each semantic telemetry event as one concise line."""

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
