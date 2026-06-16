"""common.events — the unified agent event spine.

One ordered async stream: producers ``emit`` events, subscribers consume them in
priority order. Converges what used to be three separate mechanisms (stream
sink, session recorder, hook fire-sites) onto a single decoupled bus.

Public surface:
  * :class:`EventBus` + the active-bus contextvar (:func:`set_bus` /
    :func:`current_bus` / :func:`emit_event` / :func:`emit_event_sync`).
  * The :mod:`~metagpt.common.events.types` tagged-union event dataclasses.
  * The outcome fold (re-exported from the hook layer).
"""

from metagpt.common.events.bus import EventBus
from metagpt.common.events.context import current_bus, emit_event, emit_event_sync, set_bus
from metagpt.common.events.log_subscriber import LogSubscriber
from metagpt.common.events.outcome import EMPTY, EventOutcome, HookOutcome, fold
from metagpt.common.events.types import (
    COMPACTION_CHECKPOINT,
    DIAGNOSTICS,
    FILE_CHANGED,
    FILE_MUTATED,
    FILE_SNAPSHOT,
    LLM_STREAM_DELTA,
    LLM_STREAM_END,
    MESSAGE_APPENDED,
    POST_COMPACT,
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    RECOVERY,
    RESOURCE_REPORT,
    SESSION_END,
    SESSION_START,
    TASK_PROGRESS,
    TURN_END,
    TURN_START,
    USER_PROMPT_SUBMIT,
    AgentEvent,
    CompactionCheckpointEvent,
    DiagnosticsEvent,
    FileChangedEvent,
    FileMutatedEvent,
    FileSnapshotEvent,
    LLMStreamDeltaEvent,
    LLMStreamEndEvent,
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

__all__ = [
    # bus + context
    "EventBus",
    "set_bus",
    "current_bus",
    "emit_event",
    "emit_event_sync",
    # subscribers
    "LogSubscriber",
    # outcome
    "EventOutcome",
    "HookOutcome",
    "fold",
    "EMPTY",
    # events
    "AgentEvent",
    "SessionStartEvent",
    "SessionEndEvent",
    "TurnStartEvent",
    "TurnEndEvent",
    "MessageAppendedEvent",
    "LLMStreamDeltaEvent",
    "LLMStreamEndEvent",
    "CompactionCheckpointEvent",
    "FileSnapshotEvent",
    "FileChangedEvent",
    "FileMutatedEvent",
    "DiagnosticsEvent",
    "RecoveryEvent",
    "TaskProgressEvent",
    "ResourceReportEvent",
    "UserPromptSubmitEvent",
    "PreToolUseEvent",
    "PostToolUseEvent",
    "PreCompactEvent",
    "PostCompactEvent",
    # discriminators
    "SESSION_START",
    "SESSION_END",
    "TURN_START",
    "TURN_END",
    "MESSAGE_APPENDED",
    "LLM_STREAM_DELTA",
    "LLM_STREAM_END",
    "COMPACTION_CHECKPOINT",
    "FILE_SNAPSHOT",
    "FILE_CHANGED",
    "FILE_MUTATED",
    "DIAGNOSTICS",
    "RECOVERY",
    "TASK_PROGRESS",
    "RESOURCE_REPORT",
    "USER_PROMPT_SUBMIT",
    "PRE_TOOL_USE",
    "POST_TOOL_USE",
    "PRE_COMPACT",
    "POST_COMPACT",
]
