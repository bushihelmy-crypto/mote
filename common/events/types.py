"""AgentEvent — the in-memory signals that flow on the unified event spine.

An **open** tagged union (discriminated by the ``name`` ClassVar) carrying the
agent's lifecycle signals. The set is intentionally open: a new event never
breaks an existing subscriber (subscribers consume selectively), so adding one
is a pure leaf extension.

Crucially, an event carries **no control/observation marker** — that plane is a
property of the *subscriber*, not the event (see
``common/interface/event_subscriber.py``). The same event (e.g. a tool-use) is
routed to a :class:`ControlSubscriber` (a hook that may veto/mutate, phase 1) and
to :class:`ObservationSubscriber`\\s (recorder/renderer/logger, phase 2) by the
bus. Only a control subscriber can fold a :class:`ControlOutcome`; an observer's
return is structurally dropped, so an observer can never influence the host. This
is why there is no ``is_control`` flag to keep in sync — influence is enforced by
*where a subscriber is registered*, not by an advisory boolean on the data.

These are pure data — they name *what happened*, not *who consumes it*. Each
subscriber owns the translation from a bus event to its own sink shape (the
:class:`RecorderSubscriber` maps to ``session/events.py`` records; the
:class:`HookSubscriber` maps to ``HookManager.fire`` calls). Keeping the events
free of consumer knowledge is what lets a new frontend subscribe to the same
stream without touching producers.

Organized by domain below: session · turn · message · llm · compaction · file ·
diagnostics · recovery · task · resource · lifecycle · trace · tool. The names
already carry the domain prefix; the section headers are navigational only.

Leaf module: imports only ``dataclasses``/``typing`` plus (under TYPE_CHECKING)
the ``Message`` type, so it sits at the very bottom of the layering.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, ClassVar, List, Optional

if TYPE_CHECKING:
    from metagpt.common.schema import Message, PermissionFacts

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
LLM_REQUEST = "llm_request"
LLM_RESPONSE = "llm_response"
LLM_ERROR = "llm_error"
COMPACTION_CHECKPOINT = "compaction_checkpoint"
FILE_SNAPSHOT = "file_snapshot"
USER_PROMPT_SUBMIT = "user_prompt_submit"
PRE_TOOL_USE = "pre_tool_use"
POST_TOOL_USE = "post_tool_use"
PRE_COMPACT = "pre_compact"
POST_COMPACT = "post_compact"
PRE_AGENT_SPAWN = "pre_agent_spawn"
FILE_CHANGED = "file_changed"
FILE_MUTATED = "file_mutated"
DIAGNOSTICS = "diagnostics"
RECOVERY = "recovery"
TASK_PROGRESS = "task_progress"
RESOURCE_REPORT = "resource_report"
AGENT_LIFECYCLE = "agent_lifecycle"
SPAN_START = "span_start"
SPAN_END = "span_end"


# ---------------------------------------------------------------------------
# Rewrite provenance — a control subscriber may rewrite a mutable field of a
# control event (tool args, tool output). Each such change is recorded as an
# immutable :class:`Rewrite` on the event itself, so a rewrite is traceable
# from the event alone — who changed which field, from what to what. The
# provenance rides on the event (self-describing), never in a side table that
# could drift from the value it describes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rewrite:
    """One field mutation applied to a control event by a named subscriber.

    ``field`` is the event attribute rewritten; ``before``/``after`` are its
    values around the change; ``by`` is the rewriting subscriber's ``name``,
    stamped by the bus at the single point that pairs a subscriber with the
    event it just mutated. Immutable: a recorded rewrite is history.
    """

    field: str = ""
    before: Any = None
    after: Any = None
    by: str = ""


@dataclass
class Rewritable:
    """Mixin for a control event whose fields a subscriber may rewrite.

    Carries the ordered provenance log and the *single* generic mutation
    primitive :meth:`rewrite`, which reads the before-image and appends a
    :class:`Rewrite` in one step — so a rewrite can never be applied without
    being recorded. An event becomes rewritable by inheriting this alone; it
    hand-rolls no per-field ``rebind_*`` method, and the one recording point
    serves every rewritable event, present and future.
    """

    #: Ordered log of every rewrite applied as the event flowed through the
    #: control bucket — the audit trail an observer reads off the final event.
    rewrites: tuple[Rewrite, ...] = ()

    def rewrite(self, field: str, after: Any, *, by: str = "") -> "Rewritable":
        """Return a copy with ``field`` set to ``after`` and the change recorded.

        The before-image is read here rather than supplied, so provenance is
        captured atomically with the mutation and cannot be forged or forgotten.
        ``by`` is the rewriting subscriber's name (the bus supplies it). Any
        non-rewritten field (e.g. a tool-bound closure) is preserved by
        :func:`~dataclasses.replace`.
        """
        record = Rewrite(field=field, before=getattr(self, field), after=after, by=by)
        return replace(self, **{field: after}, rewrites=(*self.rewrites, record))


# ---------------------------------------------------------------------------
# Fan-out events — no control subscriber maps these, so they reach observers
# only (recorder / renderer / logger / tracing). The bus folds nothing for them.
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

@dataclass
class SessionEndEvent:
    """The session is tearing down."""

    session_id: str = ""

    name: ClassVar[str] = SESSION_END

@dataclass
class TurnStartEvent:
    """A react turn is starting."""

    turn_id: str = ""

    name: ClassVar[str] = TURN_START

@dataclass
class TurnEndEvent:
    """A react turn finished. Carries the per-turn runtime snapshot."""

    turn_id: str = ""
    working_dir: str = ""
    model: Optional[str] = None
    token_state: Optional[dict] = None

    name: ClassVar[str] = TURN_END

@dataclass
class MessageAppendedEvent:
    """A message was appended to the stored history."""

    message: "Message" = None  # type: ignore[assignment]

    name: ClassVar[str] = MESSAGE_APPENDED

@dataclass
class LLMStreamDeltaEvent:
    """One streamed token (or chunk) from the LLM client."""

    token: str = ""

    name: ClassVar[str] = LLM_STREAM_DELTA

@dataclass
class LLMStreamEndEvent:
    """The current LLM stream finished (turn boundary for the renderer)."""

    name: ClassVar[str] = LLM_STREAM_END

@dataclass
class LLMRequestEvent:
    """A single LLM completion request is about to be issued.

    Emitted at the one LLM-call chokepoint (``BaseLLM._run_with_recovery``) right
    before the provider is hit, carrying enough to open an external trace
    (model/provider/input). One is emitted per recovery attempt, so retries /
    credential rotations / fallbacks each get their own request → response|error
    pair correlated by ``request_id``.
    """

    request_id: str = ""
    model: str = ""
    provider: str = ""  # api_type, e.g. "openai" / "anthropic"
    messages: List[Any] = field(default_factory=list)  # input wire messages
    stream: bool = False
    # Explicit trace linkage (carried in the event, not via ambient context):
    # the span this generation nests under + the run's trace_id. Stamped at the
    # emit site (``BaseLLM._call``) from the framework-native trace context.
    parent_span_id: Optional[str] = None
    trace_id: str = ""

    name: ClassVar[str] = LLM_REQUEST

@dataclass
class LLMResponseEvent:
    """A single LLM completion returned — its output, usage, cost and latency.

    The response half of :class:`LLMRequestEvent` (paired by ``request_id``).
    ``usage`` is a :meth:`~metagpt.router.cost.usage.TokenUsage.to_dict` mapping
    and ``cost_usd`` the per-call USD cost (from the cost tracker), so a
    subscriber can persist per-request token/cost or mirror it to an external
    observability backend without re-counting.
    """

    request_id: str = ""
    model: str = ""
    content: str = ""
    tool_calls: List[dict] = field(default_factory=list)  # [{id,name,arguments}]
    usage: Optional[dict] = None
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    trace_id: str = ""  # correlation symmetry with the request

    name: ClassVar[str] = LLM_RESPONSE

@dataclass
class LLMErrorEvent:
    """An LLM completion attempt raised (paired with a prior LLMRequestEvent).

    Lets a subscriber mark the matching external trace as errored and record the
    latency-to-failure. The recovery loop's own control flow (retry / rotate /
    re-raise) stays the source of truth; this only mirrors *that it failed*.
    """

    request_id: str = ""
    model: str = ""
    error_type: str = ""
    error: str = ""
    latency_ms: float = 0.0
    trace_id: str = ""  # correlation symmetry with the request

    name: ClassVar[str] = LLM_ERROR

@dataclass
class CompactionCheckpointEvent:
    """A compaction rebuilt the history — the new history + its summary."""

    messages: List["Message"] = field(default_factory=list)
    summary: str = ""

    name: ClassVar[str] = COMPACTION_CHECKPOINT

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

@dataclass
class FileChangedEvent:
    """A watched file changed on disk (file-watcher)."""

    path: str = ""
    change_type: str = ""
    mtime: float = 0.0
    size: int = 0

    name: ClassVar[str] = FILE_CHANGED

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

@dataclass
class AgentLifecycleEvent:
    """An agent crossed a residency/control-plane boundary.

    The orchestration layer (control / residency) runs *outside* any per-turn
    bus, so it owns a runtime-level bus and emits these milestones onto it:
    ``added`` / ``rehydrated`` / ``evicted`` / ``interrupted``.
    """

    session_id: str = ""
    phase: str = ""
    detail: str = ""

    name: ClassVar[str] = AGENT_LIFECYCLE

@dataclass
class SpanStartEvent:
    """A trace span opened (framework-native instrumentation primitive).

    Carries explicit trace structure — ``span_id`` / ``parent_span_id`` /
    ``trace_id`` — so the trace tree is rebuilt downstream from these IDs, not
    from any backend's ambient context. Emitted by the ``span`` contextmanager
    (:mod:`~metagpt.common.events.trace`). The instance field is ``label`` (the
    human name) — ``name`` is the reserved discriminator ClassVar.
    """

    span_id: str = ""
    parent_span_id: Optional[str] = None
    trace_id: str = ""
    label: str = ""
    attributes: dict = field(default_factory=dict)

    name: ClassVar[str] = SPAN_START

@dataclass
class SpanEndEvent:
    """A trace span closed (paired with :class:`SpanStartEvent` by ``span_id``)."""

    span_id: str = ""
    trace_id: str = ""
    status: str = "ok"  # ok | error
    error: str = ""
    attributes: dict = field(default_factory=dict)

    name: ClassVar[str] = SPAN_END


# ---------------------------------------------------------------------------
# Hook-routed events — a control subscriber (the lone HookSubscriber) maps these
# in phase 1 and may veto / mutate args / inject context / stop. They still also
# reach observers in phase 2 (rendered, logged, persisted). Being "hook-routed"
# is about which subscriber consumes them, not a property carried on the data.
# ---------------------------------------------------------------------------


@dataclass
class UserPromptSubmitEvent:
    """The user submitted a prompt for this turn."""

    prompt: str = ""

    name: ClassVar[str] = USER_PROMPT_SUBMIT

@dataclass
class PreToolUseEvent(Rewritable):
    """A tool is about to run (a subscriber may deny / mutate args).

    ``resolve_facts`` is the seam that lets a permission gate run as a control
    subscriber without the bus/subscriber layer importing tools: the executor —
    which *does* own the tool — attaches a closure that derives the tool-specific
    :class:`~metagpt.common.schema.PermissionFacts` from a given argument dict.
    A subscriber evaluates the call by calling ``resolve_facts(self.tool_input)``,
    so it always sees the facts for the *current* (possibly already-rewritten)
    args. ``None`` when no gate is wired (nothing to resolve).

    Because control subscribers run in sequence and each may rewrite the args,
    the bus threads the running args forward with :meth:`Rewritable.rewrite`
    (``field="tool_input"``): subscriber *i+1* observes the arguments as
    rewritten by subscriber *i*, and each rewrite is recorded in ``rewrites``.
    The tool-bound ``resolve_facts`` closure is preserved across a rewrite (it
    reads whatever args it is handed).
    """

    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_use_id: Optional[str] = None
    #: Tool-bound, args-agnostic fact resolver (executor-supplied). Excluded from
    #: equality/repr — it is behavior, not data.
    resolve_facts: Optional[Callable[[dict], "PermissionFacts"]] = field(
        default=None, compare=False, repr=False
    )

    name: ClassVar[str] = PRE_TOOL_USE

@dataclass
class PostToolUseEvent(Rewritable):
    """A tool finished (a subscriber may inject context / rewrite output / block).

    ``tool_response`` is the tool's result text. A control subscriber may rewrite
    it (truncate/redact) by returning ``ToolResultOutcome.updated_response``; the
    bus threads that forward with :meth:`Rewritable.rewrite`
    (``field="tool_response"``) so a later subscriber sees the already-rewritten
    output and the change is recorded in ``rewrites``, mirroring how
    :class:`PreToolUseEvent` threads ``updated_args``.
    """

    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_response: Any = None
    tool_use_id: Optional[str] = None

    name: ClassVar[str] = POST_TOOL_USE

@dataclass
class PreCompactEvent:
    """About to compact (a subscriber may veto or supply instructions)."""

    trigger: str = "auto"

    name: ClassVar[str] = PRE_COMPACT

@dataclass
class PostCompactEvent:
    """A compaction just happened (carries the summary)."""

    trigger: str = "auto"
    summary: str = ""

    name: ClassVar[str] = POST_COMPACT

@dataclass
class PreAgentSpawnEvent:
    """A child agent is about to be born (a subscriber may deny the spawn).

    Emitted by the single birth channel (``AgentControl.spawn_agent``) *before*
    any slot is reserved, carrying the resolved lineage facts a gate needs to
    judge the spawn: the parent's path, the child's would-be depth, the
    effective ``max_depth`` ceiling, and the requested role/nickname. The
    depth-limit veto — previously a direct ``raise AgentLimitReached`` wedged
    into the spawn method — now runs as a control subscriber
    (:class:`~metagpt.environment.spawn_gate.SpawnGate`, fail-closed) so it is a
    first-class, foldable, composable influence on the plane rather than hidden
    imperative glue. A ``deny`` outcome is translated back into
    :class:`AgentLimitReached` by the emitter.
    """

    parent_path: str = ""
    child_depth: int = 0
    max_depth: Optional[int] = None
    agent_role: str = ""
    nickname: str = ""

    name: ClassVar[str] = PRE_AGENT_SPAWN

#: Any concrete event (all expose a ``.name`` discriminator ClassVar).
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
    "LLM_REQUEST",
    "LLM_RESPONSE",
    "LLM_ERROR",
    "COMPACTION_CHECKPOINT",
    "FILE_SNAPSHOT",
    "USER_PROMPT_SUBMIT",
    "PRE_TOOL_USE",
    "POST_TOOL_USE",
    "PRE_COMPACT",
    "POST_COMPACT",
    "PRE_AGENT_SPAWN",
    "FILE_CHANGED",
    "FILE_MUTATED",
    "DIAGNOSTICS",
    "RECOVERY",
    "TASK_PROGRESS",
    "RESOURCE_REPORT",
    "AGENT_LIFECYCLE",
    "SPAN_START",
    "SPAN_END",
    # rewrite provenance
    "Rewrite",
    "Rewritable",
    # observation events
    "SessionStartEvent",
    "SessionEndEvent",
    "TurnStartEvent",
    "TurnEndEvent",
    "MessageAppendedEvent",
    "LLMStreamDeltaEvent",
    "LLMStreamEndEvent",
    "LLMRequestEvent",
    "LLMResponseEvent",
    "LLMErrorEvent",
    "CompactionCheckpointEvent",
    "FileSnapshotEvent",
    "FileChangedEvent",
    "FileMutatedEvent",
    "DiagnosticsEvent",
    "RecoveryEvent",
    "TaskProgressEvent",
    "ResourceReportEvent",
    "AgentLifecycleEvent",
    "SpanStartEvent",
    "SpanEndEvent",
    # control events
    "UserPromptSubmitEvent",
    "PreToolUseEvent",
    "PostToolUseEvent",
    "PreCompactEvent",
    "PostCompactEvent",
    "PreAgentSpawnEvent",
    # union
    "AgentEvent",
]
