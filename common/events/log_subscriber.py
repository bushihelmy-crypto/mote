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

from metagpt.common.events.types import (
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
from metagpt.common.logs import logger

def _clip(text: str) -> str:
    return " ".join(str(text).split())


class LogSubscriber:
    """Logs each semantic bus event as one concise line (observation-only)."""

    #: Last (after recorder at 80) — purely cosmetic since it folds nothing, but
    #: it reads cleanly as "log what finally happened".
    priority: int = 90

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
        # Milestone events at INFO; routine per-turn events at DEBUG.
        if isinstance(event, SessionStartEvent):
            logger.info(
                f"event session_start id={event.session_id[:8] or '?'} "
                f"source={event.source} model={event.model or '?'}"
            )
        elif isinstance(event, SessionEndEvent):
            logger.info(f"event session_end id={event.session_id[:8] or '?'}")
        elif isinstance(event, CompactionCheckpointEvent):
            logger.info(
                f"event compaction_checkpoint messages={len(event.messages)} "
                f"summary='{_clip(event.summary)}'"
            )
        elif isinstance(event, PreCompactEvent):
            logger.info(f"event pre_compact trigger={event.trigger}")
        elif isinstance(event, PostCompactEvent):
            logger.info(f"event post_compact trigger={event.trigger}")
        elif isinstance(event, TurnStartEvent):
            logger.debug(f"event turn_start turn={event.turn_id or '?'}")
        elif isinstance(event, TurnEndEvent):
            logger.debug(f"event turn_end turn={event.turn_id or '?'} model={event.model or '?'}")
        elif isinstance(event, MessageAppendedEvent):
            msg = event.message
            role = getattr(msg, "role", "?")
            content = getattr(msg, "content", "") or ""
            logger.debug(f"event message_appended role={role} chars={len(content)} '{_clip(content)}'")
        elif isinstance(event, UserPromptSubmitEvent):
            logger.debug(f"event user_prompt_submit '{_clip(event.prompt)}'")
        elif isinstance(event, PreToolUseEvent):
            logger.debug(f"event pre_tool_use tool={event.tool_name}")
        elif isinstance(event, PostToolUseEvent):
            logger.debug(f"event post_tool_use tool={event.tool_name}")
        elif isinstance(event, FileSnapshotEvent):
            logger.debug(
                f"event file_snapshot op={event.operation} backend={event.backend} "
                f"path={event.display_path or event.path}"
            )
        elif isinstance(event, FileChangedEvent):
            logger.debug(f"event file_changed type={event.change_type} path={event.path}")
        elif isinstance(event, DiagnosticsEvent):
            logger.debug(f"event diagnostics files={len(event.paths)} chars={len(event.block)}")
        elif isinstance(event, AgentLifecycleEvent):
            detail = f" {event.detail}" if event.detail else ""
            logger.info(
                f"event agent_lifecycle phase={event.phase} "
                f"id={event.session_id[:8] or '?'}{detail}"
            )
        elif isinstance(event, RecoveryEvent):
            logger.info(
                f"event recovery phase={event.phase} action={event.action} "
                f"attempt={event.attempt} error={event.error_type}: '{event.error}'"
            )
        elif isinstance(event, ResourceReportEvent):
            logger.debug(
                f"event resource_report block={event.block} name={event.name_} role={event.role or '?'}"
            )
        else:
            logger.debug(f"event {getattr(event, 'name', '?')}")


__all__ = ["LogSubscriber"]
