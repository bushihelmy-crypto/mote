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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Generic, List, Optional, TypeVar, get_args, get_origin

from mote.common.events.outcomes import (
    CompactOutcome,
    PromptOutcome,
    SpawnOutcome,
    ToolCallOutcome,
    ToolResultOutcome,
    TurnOutcome,
)
from mote.common.events.rewrite import Rewritable, Rewrite
from mote.common.interface.event_subscriber import ControlOutcome

if TYPE_CHECKING:
    from mote.common.exception import ErrorReport
    from mote.common.schema import Message, PermissionFacts

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
LLM_RETRY = "llm_retry"
COMPACTION_CHECKPOINT = "compaction_checkpoint"
HISTORY_EDITED = "history_edited"
FILE_SNAPSHOT = "file_snapshot"
USER_PROMPT_SUBMIT = "user_prompt_submit"
PRE_TOOL_USE = "pre_tool_use"
POST_TOOL_USE = "post_tool_use"
PRE_COMPACT = "pre_compact"
POST_COMPACT = "post_compact"
PRE_AGENT_SPAWN = "pre_agent_spawn"
FILE_CHANGED = "file_changed"
FILE_MUTATED = "file_mutated"
TOOLS_CHANGED = "tools_changed"
DIAGNOSTICS = "diagnostics"
RECOVERY = "recovery"
BREAKER_STATE_CHANGE = "breaker_state_change"
TASK_PROGRESS = "task_progress"
RESOURCE_REPORT = "resource_report"
AGENT_LIFECYCLE = "agent_lifecycle"
SPAN_START = "span_start"
SPAN_END = "span_end"
BUDGET = "budget"
ACTIVITY_STARTED = "activity_started"
ACTIVITY_COMPLETED = "activity_completed"
JOURNAL = "journal"


# ---------------------------------------------------------------------------
# Rewrite provenance — a control subscriber may rewrite a mutable field of a
# control event (tool args, tool output). :class:`Rewrite`/:class:`Rewritable`
# live in the ``common/events/rewrite.py`` leaf (so ``outcome_type`` can bind
# events to outcomes without a cycle); re-exported here for convenience.
# ---------------------------------------------------------------------------


#: The outcome type a control event is bound to — the CRTP-style parameter that
#: statically links an event to *its* outcome. ``PreToolUseEvent`` subclasses
#: ``ControlEvent[ToolCallOutcome]``, so ``bus.emit(PreToolUseEvent())`` infers
#: ``ToolCallOutcome | None`` (see ``bus.py``) with no cast at the call site.
_TOut = TypeVar("_TOut", bound=ControlOutcome)


class ControlEvent(Generic[_TOut]):
    """Generic base for every *control* event — parametrized on its outcome type.

    A control event is one a :class:`ControlSubscriber` may fold a
    :class:`ControlOutcome` for (veto / rewrite / inject / stop). Declaring the
    outcome type as the generic argument (``ControlEvent[ToolCallOutcome]``) is
    the single source of truth for the event↔outcome link:

    * **Static** — ``bus.emit(event: ControlEvent[O]) -> Optional[O]`` threads the
      argument through, so a call site reads the exact outcome type with no cast.
      ``tool_outcome = ToolResultOutcome(...)`` returned for a ``PreToolUseEvent``
      is a compile-time error.
    * **Runtime** — the ``outcome_type`` ClassVar (which ``bus.py`` reads for its
      ``isinstance`` defence-in-depth) is *auto-derived* from the same generic
      argument in :meth:`__init_subclass__`, so the two can never drift: there is
      no separate ``outcome_type = XOutcome`` line to forget to update.

    A pure-observation event does **not** inherit this (it has no outcome); the
    bus's ``emit`` overload returns ``None`` for it. Events that are also
    :class:`Rewritable` inherit both (``ControlEvent[O], Rewritable``).
    """

    name: ClassVar[str] = ""
    #: Auto-derived from the generic argument at subclass creation — never set by
    #: hand. Read by ``bus.py`` for its runtime ``isinstance`` outcome check.
    outcome_type: ClassVar[type[ControlOutcome]]

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        # Extract the outcome type from ``ControlEvent[X]`` in the MRO bases and
        # pin it onto the ClassVar the bus reads. A subclass that specializes the
        # parameter (the concrete events below) sets it; an intermediate generic
        # subclass that leaves it a TypeVar is skipped (stays inherited/unset).
        for base in getattr(cls, "__orig_bases__", ()):
            if get_origin(base) is ControlEvent:
                (arg,) = get_args(base)
                if isinstance(arg, type):
                    cls.outcome_type = arg
                return


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
    source: str = "startup"  # SessionStart "source" matcher (startup|resume|...)

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
class TurnEndEvent(ControlEvent[TurnOutcome]):
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
    ``usage`` is a :meth:`~mote.router.cost.usage.TokenUsage.to_dict` mapping
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
class LLMRetryEvent:
    """A transient LLM failure is about to be retried (fired from tenacity's
    ``before_sleep`` hook, once per *pending* re-issue).

    Purely observational — mirrors *that* the recovery loop will back-off and
    re-issue the same request. Carries the countdown coordinates (which attempt
    just failed, the total budget, and the chosen back-off) so a CLI can render
    a transient "retrying in Ns" line. The final, budget-exhausted failure does
    NOT emit this (no ``before_sleep`` fires); it surfaces via the turn-level
    error path instead — the transient-retry UX.
    """

    request_id: str = ""
    model: str = ""
    attempt: int = 0  # the attempt that just failed (tenacity attempt_number)
    max_attempts: int = 0  # = LLM_RETRY_ATTEMPTS
    delay_ms: float = 0.0  # tenacity's chosen next back-off duration
    error_type: str = ""
    error: str = ""
    trace_id: str = ""

    name: ClassVar[str] = LLM_RETRY


@dataclass
class CompactionCheckpointEvent:
    """A compaction rebuilt the history — the new history + its summary."""

    messages: List["Message"] = field(default_factory=list)
    summary: str = ""

    name: ClassVar[str] = COMPACTION_CHECKPOINT


@dataclass
class HistoryEditedEvent:
    """A user edited the history directly (e.g. deleted react-units) — the
    rebuilt message list, with no summary.

    Distinct from :class:`CompactionCheckpointEvent` on purpose: both are
    persisted identically (the recorder writes each as a ``CompactedEvent`` so
    replay/resume reset history to ``messages`` for free), but the view projector
    ignores this event by construction (its name is not one it folds → ``[]``),
    so a delete does NOT surface the "✻ Conversation compacted" boundary marker.
    ``reason`` records why the edit happened (``"delete"`` today) for future
    provenance; it is not rendered.
    """

    messages: List["Message"] = field(default_factory=list)
    reason: str = "delete"

    name: ClassVar[str] = HISTORY_EDITED


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
    (Write/Edit/...) succeeds, carrying the resolved path. Purely
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
class ToolsChangedEvent:
    """The executor's bound tool set changed (a tool was de-registered).

    Emitted by the :class:`ToolExecutor` when a tool is removed
    (``deregister_tool``) so downstream views refresh instead of silently
    drifting: the per-turn tool catalog drops the vanished names from its
    incremental frontier (so a later re-registration is re-announced), and the
    compaction pipeline refreshes its reconstructable-tool-name set. Purely an
    observation — the executor's live ``_tools`` map stays the source of truth;
    this only announces *that it changed* and carries the post-change facts a
    consumer needs (which names went away, and the fresh reconstructable set), so
    no consumer needs a back-reference to the executor.
    """

    removed: List[str] = field(default_factory=list)
    reconstructable: List[str] = field(default_factory=list)

    name: ClassVar[str] = TOOLS_CHANGED


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
class BreakerStateChangeEvent:
    """A resource's :class:`~mote.common.resilience.CircuitBreaker` changed state.

    Emitted (observation-only) when a breaker transitions closed→open,
    open→half_open, or half_open→closed/open, so a frontend/logger can see a
    provider being shed and recovering. The breaker's own ``admit``/``record``
    verdicts are the source of truth; this just mirrors *that a resource's health
    state flipped*. ``key`` is the opaque resource label (for LLM:
    ``api_type::model::key_index``); ``reason`` is the breaker's human note.
    """

    key: str = ""
    old_state: str = ""  # BreakerState.value
    new_state: str = ""
    reason: str = ""

    name: ClassVar[str] = BREAKER_STATE_CHANGE


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
    #: Execution lineage (``ScopePath``) this ping belongs to. ``()`` for a plain
    #: background-task progress line (today's behavior — folds to a flat
    #: ``TaskProgress`` view event). A non-empty scope whose head is an open
    #: activity routes the ping into that activity's subtree (a per-node update).
    scope: tuple = ()

    name: ClassVar[str] = TASK_PROGRESS


@dataclass
class ActivityStartedEvent:
    """A nested orchestration (a ``run_graph`` graph today; a sub-agent / bg task
    in future) began — the machine-side signal the projector folds into an
    :class:`~mote.cli.contracts.view.events.ActivityStarted` ViewEvent.

    ``scope`` identifies the activity (its :class:`~mote.common.events.scope.
    ScopePath`); ``topology`` is a neutral pre-computed structure describing the
    declared graph (plain dicts/lists, so this leaf imports nothing from bggraph).
    Purely observational — mirrors *that an activity opened* so a renderer can
    draw its shape before any node runs.
    """

    scope: tuple = ()
    activity_kind: str = ""  # "graph" | "agent" | "task"
    label: str = ""
    topology: Optional[dict] = None

    name: ClassVar[str] = ACTIVITY_STARTED


@dataclass
class ActivityCompletedEvent:
    """A nested orchestration finished — the terminal, **self-sufficient** signal.

    Carries the full outcome read straight off the graph's terminal state
    (``node_states`` + ``outcome`` + ``summary``), so a replayed / resumed
    transcript renders the outcome from this event alone, never reconstructing it
    from the live :class:`TaskProgressEvent` stream (which a replay does not
    have). ``node_states`` is a list of neutral dicts; ``outcome`` is
    ``"success"`` | ``"failed"``. Purely observational.
    """

    scope: tuple = ()
    outcome: str = "success"  # success | failed
    node_states: List[dict] = field(default_factory=list)
    summary: str = ""

    name: ClassVar[str] = ACTIVITY_COMPLETED


@dataclass
class BudgetEvent:
    """An agent crossed a spend threshold against its configured budget cap.

    Emitted by :class:`ContextProvider.enforce_budget` on the observation plane
    (fire-and-forget) when this agent's own accrued spend crosses the soft
    warning line (``stopped=False``, once) or the hard cap (``stopped=True``,
    once). The loop reads the returned verdict to actually halt; this event is
    purely for the UI/recorder to surface + persist the milestone. Only emitted
    when a positive ``max_cost`` is configured — an unbudgeted agent is silent.
    """

    spend: float = 0.0  # USD accrued by this agent so far
    limit: float = 0.0  # configured max_cost cap (USD)
    fraction: float = 0.0  # spend / limit at emit time
    stopped: bool = False  # True once the hard cap halted the loop

    name: ClassVar[str] = BUDGET


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
    (:mod:`~mote.common.events.trace`). The instance field is ``label`` (the
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


@dataclass
class JournalEvent:
    """A durable run-journal step crossed a lifecycle boundary.

    Emitted (observation-only) by the durable-execution seams
    (:class:`~mote.loop.durable.runner.DurableRunner` think steps, durable
    timers, and the EXTERNAL/LOCAL tool ledger) whenever a step is started,
    completed, failed, or reaped, so a frontend/logger can watch the otherwise
    invisible crash-resume bookkeeping (which thinks were memoized, which
    dangling calls were healed, how the long-session journal stays bounded).

    Purely a mirror: the journal's own on-disk log is the source of truth; this
    just announces *that* a record moved. ``kind`` is the step class
    (``think`` / ``tool`` / ``timer``); ``phase`` is the lifecycle transition
    (``started`` / ``completed`` / ``failed`` / ``reaped``); ``effect`` is the
    step's side-effect class (``pure`` / ``local`` / ``external``); ``step_id``
    is the journal's self-anchored key.
    """

    step_id: str = ""
    kind: str = ""  # think | tool | timer
    phase: str = ""  # started | completed | failed | reaped
    effect: str = ""  # pure | local | external
    seq: int = 0

    name: ClassVar[str] = JOURNAL


# ---------------------------------------------------------------------------
# Hook-routed events — a control subscriber (the lone HookSubscriber) maps these
# in phase 1 and may veto / mutate args / inject context / stop. They still also
# reach observers in phase 2 (rendered, logged, persisted). Being "hook-routed"
# is about which subscriber consumes them, not a property carried on the data.
# ---------------------------------------------------------------------------


@dataclass
class UserPromptSubmitEvent(ControlEvent[PromptOutcome], Rewritable):
    """The user submitted a prompt for this turn.

    :class:`Rewritable`: a control subscriber may rewrite ``prompt`` (the
    secret-upload subscriber vaults ``<secret>…</secret>`` spans and substitutes
    placeholders) by returning ``PromptOutcome.updated_prompt``; the bus threads
    that forward with :meth:`Rewritable.rewrite` (``field="prompt"``) so a later
    subscriber sees the already-rewritten prompt and the change is recorded in
    ``rewrites``, mirroring how :class:`PreToolUseEvent` threads ``updated_args``.
    """

    prompt: str = ""

    name: ClassVar[str] = USER_PROMPT_SUBMIT


@dataclass
class PreToolUseEvent(ControlEvent[ToolCallOutcome], Rewritable):
    """A tool is about to run (a subscriber may deny / mutate args).

    ``resolve_facts`` is the seam that lets a permission gate run as a control
    subscriber without the bus/subscriber layer importing tools: the executor —
    which *does* own the tool — attaches a closure that derives the tool-specific
    :class:`~mote.common.schema.PermissionFacts` from a given argument dict.
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
    resolve_facts: Optional[Callable[[dict], "PermissionFacts"]] = field(default=None, compare=False, repr=False)
    #: Execution lineage (``ScopePath``) this call runs under, pulled from the
    #: ambient scope contextvar at emit time. ``()`` = top level. Lets a scoped
    #: call (e.g. a graph node's dispatched tool, ``tool_use_id`` may be ``None``)
    #: be attributed to its parent activity instead of orphaning at the top.
    scope: tuple = ()

    name: ClassVar[str] = PRE_TOOL_USE


@dataclass
class PostToolUseEvent(ControlEvent[ToolResultOutcome], Rewritable):
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
    #: The executor's structured success fact from the ``ToolResult`` (a tool body
    #: that raised or returned ``ToolResult(success=False)``), carried verbatim so
    #: observers read the outcome instead of sniffing ``tool_response`` prefixes.
    #: This is the *tool-body* outcome at emit time; a PostToolUse-hook block that
    #: fails the call afterwards is applied by the executor post-return and is not
    #: reflected here (see tool_executor for that separate path).
    success: bool = True
    #: Structured failure record on a non-success result (``ErrorReport``), mirrored
    #: from the ``ToolResult``; ``None`` on success or for a legacy output-only fail.
    error: Optional["ErrorReport"] = None
    #: Structured media the tool produced (``list[ToolMedia]``: image/pdf artifacts),
    #: mirrored from the ``ToolResult`` so the view layer folds a media block from the
    #: fact instead of sniffing ``tool_response`` text / reverse-engineering a path.
    media: list = field(default_factory=list)
    #: Structured file modifications the tool made (``list[FileChange]``: path/old/new),
    #: mirrored from the ``ToolResult`` so the view layer renders the change from the
    #: fact — side-by-side on a rich host, a synthesized coloured diff on a text host —
    #: instead of sniffing ``tool_response`` text for a diff shape.
    file_changes: list = field(default_factory=list)
    #: Execution lineage (``ScopePath``) this call ran under — see
    #: :attr:`PreToolUseEvent.scope`. ``()`` = top level.
    scope: tuple = ()

    name: ClassVar[str] = POST_TOOL_USE


@dataclass
class PreCompactEvent(ControlEvent[CompactOutcome]):
    """About to compact (a subscriber may veto or supply instructions)."""

    trigger: str = "auto"

    name: ClassVar[str] = PRE_COMPACT


@dataclass
class PostCompactEvent:
    """A compaction just happened (carries the summary)."""

    trigger: str = "auto"
    summary: str = ""

    name: ClassVar[str] = POST_COMPACT


# The single definition of "the stored history was structurally rebuilt, so
# every piece of state DERIVED from history must be re-derived." Two disjoint
# causes, one consequence:
#   * :class:`PostCompactEvent` — a compaction condensed the *head* away (old
#     turns the user can no longer see or select).
#   * :class:`HistoryEditedEvent` — the user directly rewrote the *live* history
#     (``/clear`` empties it; deleting react-units prunes selected turns).
# The two never touch the same messages (compaction only condenses what the user
# can't reach; an edit only removes what is still live), so a source/side-store
# that caches "what I've already put into history" is stale after EITHER and
# resets identically. Incremental turn-context frontiers reset on this tuple;
# the compaction-only "history compacted" notice keys on PostCompactEvent alone.
HISTORY_RESET_EVENTS: tuple[type, ...] = (PostCompactEvent, HistoryEditedEvent)


@dataclass
class PreAgentSpawnEvent(ControlEvent[SpawnOutcome]):
    """A child agent is about to be born (a subscriber may deny the spawn).

    Emitted by the single birth channel (``AgentControl.spawn_agent``) *before*
    any slot is reserved, carrying the resolved lineage facts a gate needs to
    judge the spawn: the parent's path, the child's would-be depth, the
    effective ``max_depth`` ceiling, and the requested role/nickname. The
    depth-limit veto — previously a direct ``raise AgentLimitReached`` wedged
    into the spawn method — now runs as a control subscriber
    (:class:`~mote.environment.spawn_gate.SpawnGate`, fail-closed) so it is a
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
    "LLM_RETRY",
    "COMPACTION_CHECKPOINT",
    "HISTORY_EDITED",
    "FILE_SNAPSHOT",
    "USER_PROMPT_SUBMIT",
    "PRE_TOOL_USE",
    "POST_TOOL_USE",
    "PRE_COMPACT",
    "POST_COMPACT",
    "PRE_AGENT_SPAWN",
    "FILE_CHANGED",
    "FILE_MUTATED",
    "TOOLS_CHANGED",
    "DIAGNOSTICS",
    "RECOVERY",
    "BREAKER_STATE_CHANGE",
    "TASK_PROGRESS",
    "RESOURCE_REPORT",
    "AGENT_LIFECYCLE",
    "SPAN_START",
    "SPAN_END",
    "BUDGET",
    "ACTIVITY_STARTED",
    "ACTIVITY_COMPLETED",
    "JOURNAL",
    # rewrite provenance
    "Rewrite",
    "Rewritable",
    # control-event generic base
    "ControlEvent",
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
    "LLMRetryEvent",
    "CompactionCheckpointEvent",
    "HistoryEditedEvent",
    "FileSnapshotEvent",
    "FileChangedEvent",
    "FileMutatedEvent",
    "ToolsChangedEvent",
    "DiagnosticsEvent",
    "RecoveryEvent",
    "BreakerStateChangeEvent",
    "TaskProgressEvent",
    "BudgetEvent",
    "ResourceReportEvent",
    "AgentLifecycleEvent",
    "SpanStartEvent",
    "SpanEndEvent",
    "ActivityStartedEvent",
    "ActivityCompletedEvent",
    "JournalEvent",
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
