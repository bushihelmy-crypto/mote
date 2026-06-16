"""AgentEvent — the in-memory signals that flow on the unified event spine.

A small tagged union (discriminated by the ``name`` ClassVar) carrying the
agent's lifecycle signals. Two roles:

* **Observation** events are fire-and-forget: subscribers persist / mirror them
  and return ``None``/``EMPTY`` (the bus ignores the folded outcome).
* **Control** events let a subscriber influence the host: a hook may veto a tool
  call, mutate its args, inject context, or stop the agent. The bus folds the
  per-subscriber :class:`HookOutcome`\\s and the emitter reads the result.

These are pure data — they name *what happened*, not *who consumes it*. Each
subscriber owns the translation from a bus event to its own sink shape (the
:class:`RecorderSubscriber` maps to ``session/events.py`` records; the
:class:`HookSubscriber` maps to ``HookManager.fire`` calls). Keeping the events
free of consumer knowledge is what lets a new frontend subscribe to the same
stream without touching producers.

Leaf module: imports only ``dataclasses``/``typing`` plus (under TYPE_CHECKING)
the ``Message`` type, so it sits at the very bottom of the layering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, List, Optional

if TYPE_CHECKING:
    from metagpt.common.schema import Message

# ---------------------------------------------------------------------------
# Event-name discriminators
# ---------------------------------------------------------------------------

SESSION_START = "session_start"
SESSION_END = "session_end"
TURN_START = "turn_start"
TURN_END = "turn_end"
MESSAGE_APPENDED = "message_appended"
LLM_STREAM_DELTA = "llm_stream_delta"
LLM_STREAM_END = "llm_stream_end"
COMPACTION_CHECKPOINT = "compaction_checkpoint"
FILE_SNAPSHOT = "file_snapshot"
USER_PROMPT_SUBMIT = "user_prompt_submit"
PRE_TOOL_USE = "pre_tool_use"
POST_TOOL_USE = "post_tool_use"
PRE_COMPACT = "pre_compact"
POST_COMPACT = "post_compact"
FILE_CHANGED = "file_changed"
FILE_MUTATED = "file_mutated"
DIAGNOSTICS = "diagnostics"
RECOVERY = "recovery"
TASK_PROGRESS = "task_progress"
RESOURCE_REPORT = "resource_report"


# ---------------------------------------------------------------------------
# Observation events (subscribers return None / EMPTY)
# ---------------------------------------------------------------------------


@dataclass
class SessionStartEvent:
    """A session began (or resumed). Carries the identity to seed the rollout."""

    session_id: str = ""
    parent_session_id: Optional[str] = None
    working_dir: str = ""
    original_working_dir: str = ""
    project_root: str = ""
    model: Optional[str] = None
    role_class: Optional[str] = None
    source: str = "startup"  # CC SessionStart "source" matcher (startup|resume|...)

    name: ClassVar[str] = SESSION_START
    is_control: ClassVar[bool] = True  # also fired as a hook (SessionStart)


@dataclass
class SessionEndEvent:
    """The session is tearing down."""

    session_id: str = ""

    name: ClassVar[str] = SESSION_END
    is_control: ClassVar[bool] = False


@dataclass
class TurnStartEvent:
    """A react turn is starting."""

    turn_id: str = ""

    name: ClassVar[str] = TURN_START
    is_control: ClassVar[bool] = False


@dataclass
class TurnEndEvent:
    """A react turn finished. Carries the per-turn runtime snapshot."""

    turn_id: str = ""
    working_dir: str = ""
    model: Optional[str] = None
    token_state: Optional[dict] = None

    name: ClassVar[str] = TURN_END
    is_control: ClassVar[bool] = False


@dataclass
class MessageAppendedEvent:
    """A message was appended to the stored history."""

    message: "Message" = None  # type: ignore[assignment]

    name: ClassVar[str] = MESSAGE_APPENDED
    is_control: ClassVar[bool] = False


@dataclass
class LLMStreamDeltaEvent:
    """One streamed token (or chunk) from the LLM client."""

    token: str = ""

    name: ClassVar[str] = LLM_STREAM_DELTA
    is_control: ClassVar[bool] = False


@dataclass
class LLMStreamEndEvent:
    """The current LLM stream finished (turn boundary for the renderer)."""

    name: ClassVar[str] = LLM_STREAM_END
    is_control: ClassVar[bool] = False


@dataclass
class CompactionCheckpointEvent:
    """A compaction rebuilt the history — the new history + its summary."""

    messages: List["Message"] = field(default_factory=list)
    summary: str = ""

    name: ClassVar[str] = COMPACTION_CHECKPOINT
    is_control: ClassVar[bool] = False


@dataclass
class FileSnapshotEvent:
    """A before-image snapshot of a file a tool is about to mutate."""

    path: str = ""
    operation: str = "update"
    pre_hash: Optional[str] = None
    pre_size: int = 0
    display_path: str = ""
    tool: str = ""
    backend: str = "blob"

    name: ClassVar[str] = FILE_SNAPSHOT
    is_control: ClassVar[bool] = False


@dataclass
class FileChangedEvent:
    """A watched file changed on disk (file-watcher)."""

    path: str = ""
    change_type: str = ""
    mtime: float = 0.0
    size: int = 0

    name: ClassVar[str] = FILE_CHANGED
    is_control: ClassVar[bool] = True  # routed to the FileChanged hook


@dataclass
class FileMutatedEvent:
    """A tool just successfully wrote/created/deleted a file on disk.

    Emitted by the :class:`ToolExecutor` right after a filesystem-mutating tool
    (Write/Edit/NotebookEdit/...) succeeds, carrying the resolved path. Purely
    an observation: subscribers react (the file-watcher records it as a
    *self-write* so its next poll doesn't echo our own edit back as an external
    change; future consumers could track changed files / auto-stage). Distinct
    from :class:`FileChangedEvent`, which the watcher emits for *external*
    changes it detects by polling.
    """

    path: str = ""
    tool: str = ""
    operation: str = "update"  # create / update / delete (best-effort)

    name: ClassVar[str] = FILE_MUTATED
    is_control: ClassVar[bool] = False


@dataclass
class DiagnosticsEvent:
    """Language-server diagnostics changed after a file sync.

    Emitted by the :class:`LspService` once a synced edit yields a *changed*
    diagnostic set, carrying a pre-rendered context ``block`` and the affected
    ``paths``. Purely an observation: the diagnostics buffer accumulates the
    block for next-turn context injection; future subscribers (a status line,
    an error counter, an auto-fix agent) can react to the same signal without
    the producer naming them. The output counterpart of
    :class:`FileMutatedEvent` (the input that triggers the sync).
    """

    block: str = ""
    paths: List[str] = field(default_factory=list)

    name: ClassVar[str] = DIAGNOSTICS
    is_control: ClassVar[bool] = False


@dataclass
class RecoveryEvent:
    """A retry/recovery loop attempt resolved (recovered or gave up).

    Emitted by the generic :class:`RecoveryRunner` so any frontend/logger can
    observe the retry/rotate/fallback/compress decisions that otherwise stay
    invisible inside the loop. Purely an observation — the runner's own control
    flow (the eventual re-raise / retry) is the real source of truth; this just
    mirrors *what the loop decided*.
    """

    phase: str = "recovered"  # recovered | give_up
    action: str = ""  # RecoveryAction.value (retry / rotate_credential / ...)
    attempt: int = 0
    error_type: str = ""
    error: str = ""

    name: ClassVar[str] = RECOVERY
    is_control: ClassVar[bool] = False


@dataclass
class TaskProgressEvent:
    """A background task reported a progress line (already rendered).

    Emitted by the bggraph progress writer alongside the disk append (the
    :class:`TaskAttachmentGenerator` disk output stays the source of truth);
    this lets subscribers mirror live progress without polling the store.
    """

    task_id: str = ""
    stage: str = ""
    status: str = ""
    detail: str = ""  # rendered, no trailing newline

    name: ClassVar[str] = TASK_PROGRESS
    is_control: ClassVar[bool] = False


@dataclass
class ResourceReportEvent:
    """A non-streaming resource observation a reporter pushed to the UI.

    Emitted by :class:`ResourceReporter` in place of its old direct HTTP POST;
    the :class:`ReporterSubscriber` (when wired) reconstructs the payload and
    POSTs it. ``name_`` is suffixed to avoid clashing with the ``name`` ClassVar
    discriminator every event carries.
    """

    block: str = ""
    name_: str = ""
    value: Any = None
    extra: Optional[dict] = None
    uuid: str = ""
    role: Optional[str] = None

    name: ClassVar[str] = RESOURCE_REPORT
    is_control: ClassVar[bool] = False


# ---------------------------------------------------------------------------
# Control events (subscribers may return a non-empty outcome -> folded)
# ---------------------------------------------------------------------------


@dataclass
class UserPromptSubmitEvent:
    """The user submitted a prompt for this turn."""

    prompt: str = ""

    name: ClassVar[str] = USER_PROMPT_SUBMIT
    is_control: ClassVar[bool] = True


@dataclass
class PreToolUseEvent:
    """A tool is about to run (a subscriber may deny / mutate args)."""

    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_use_id: Optional[str] = None

    name: ClassVar[str] = PRE_TOOL_USE
    is_control: ClassVar[bool] = True


@dataclass
class PostToolUseEvent:
    """A tool finished (a subscriber may inject context / block)."""

    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_response: Any = None
    tool_use_id: Optional[str] = None

    name: ClassVar[str] = POST_TOOL_USE
    is_control: ClassVar[bool] = True


@dataclass
class PreCompactEvent:
    """About to compact (a subscriber may veto or supply instructions)."""

    trigger: str = "auto"

    name: ClassVar[str] = PRE_COMPACT
    is_control: ClassVar[bool] = True


@dataclass
class PostCompactEvent:
    """A compaction just happened (carries the summary)."""

    trigger: str = "auto"
    summary: str = ""

    name: ClassVar[str] = POST_COMPACT
    is_control: ClassVar[bool] = True


#: Any concrete event (all expose ``.name`` + ``.is_control``).
AgentEvent = Any


__all__ = [
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
    "USER_PROMPT_SUBMIT",
    "PRE_TOOL_USE",
    "POST_TOOL_USE",
    "PRE_COMPACT",
    "POST_COMPACT",
    "FILE_CHANGED",
    "FILE_MUTATED",
    "DIAGNOSTICS",
    "RECOVERY",
    "TASK_PROGRESS",
    "RESOURCE_REPORT",
    # observation events
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
    # control events
    "UserPromptSubmitEvent",
    "PreToolUseEvent",
    "PostToolUseEvent",
    "PreCompactEvent",
    "PostCompactEvent",
    # union
    "AgentEvent",
]
