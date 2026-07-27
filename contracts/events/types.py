"""Typed runtime events used as durable facts or telemetry observations.

An **open** tagged union (discriminated by the ``name`` ClassVar) carrying the
agent's lifecycle signals. The set is intentionally open: a new event never
breaks an existing subscriber (subscribers consume selectively), so adding one
is a pure leaf extension.

Events are immutable facts. They carry no control marker or outcome, and a
subscriber can never influence the producer through the fact stream. Any
pre-action decision belongs to a domain Policy and its typed Intent/Decision.

These are pure data — they name *what happened*, not *who consumes it*.
Session-owned durable facts cross an explicit commit boundary; telemetry
subscribers consume selectively without changing producer outcomes.

Organized by domain below: session · turn · message · llm · compaction · file ·
diagnostics · recovery · task · resource · lifecycle · trace · tool. The names
already carry the domain prefix; the section headers are navigational only.

Leaf module: imports only ``dataclasses``/``typing`` plus (under TYPE_CHECKING)
the ``Message`` type, so it sits at the very bottom of the layering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, List, Literal, Optional, Self

from mote.contracts.fileops.models import FileChangeAttribution, FileChangeKind, FileVersion

if TYPE_CHECKING:
    from mote.contracts.artifacts import ArtifactRef
    from mote.contracts.errors import ErrorReport
    from mote.contracts.schema import Message

# ---------------------------------------------------------------------------
# Event-name discriminators
# ---------------------------------------------------------------------------

SESSION_START = "session_start"
SESSION_END = "session_end"
TURN_START = "turn_start"
TURN_END = "turn_end"
MESSAGE_APPENDED = "message_appended"
LLM_STREAM_DELTA = "llm_stream_delta"
LLM_STREAM_COMMITTED = "llm_stream_committed"
LLM_STREAM_DISCARDED = "llm_stream_discarded"
LLM_STREAM_INTERRUPTED = "llm_stream_interrupted"
LLM_STREAM_END = "llm_stream_end"
MODEL_CALL_PLANNED = "model_call_planned"
MODEL_ATTEMPT_ADMISSION_REJECTED = "model_attempt_admission_rejected"
MODEL_ATTEMPT_STARTED = "model_attempt_started"
MODEL_ATTEMPT_FINISHED = "model_attempt_finished"
MODEL_FALLBACK_SELECTED = "model_fallback_selected"
MODEL_CALL_FINISHED = "model_call_finished"
ROUTING_DECISION = "routing_decision"
CONTEXT_COMPACTED = "context_compacted"
HISTORY_EDITED = "history_edited"
USER_PROMPT_SUBMIT = "user_prompt_submit"
PROMPT_REJECTED = "prompt_rejected"
TOOL_INVOCATION_STARTED = "tool_invocation_started"
TOOL_CALL_FINISHED = "tool_call_finished"
POST_COMPACT = "post_compact"
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
OUTPUT_CANDIDATE_RECEIVED = "output_candidate_received"
OUTPUT_VALIDATION_REJECTED = "output_validation_rejected"
OUTPUT_ACCEPTED = "output_accepted"
OUTPUT_MIGRATED = "output_migrated"
OUTPUT_COMMIT_STARTED = "output_commit_started"
OUTPUT_COMMITTED = "output_committed"
OUTPUT_PUBLICATION_QUEUED = "output_publication_queued"
OUTPUT_PUBLISHED = "output_published"
OUTPUT_SNAPSHOT = "output_snapshot"
OUTPUT_SNAPSHOT_INVALIDATED = "output_snapshot_invalidated"
RUN_LEASE = "run_lease"
RUNTIME_DURABILITY_CHANGED = "runtime_durability_changed"


class _DurableFact:
    """Canonical JSON payload behavior for facts that enter the journal."""

    def payload(self) -> dict[str, Any]:
        return dict(vars(self))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        names = set(getattr(cls, "__dataclass_fields__", {}))
        return cls(**{key: value for key, value in payload.items() if key in names})


# ---------------------------------------------------------------------------
# Observation facts
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
class TurnEndEvent:
    """A react turn finished. Carries the per-turn runtime snapshot."""

    turn_id: str = ""
    working_dir: str = ""
    model: Optional[str] = None
    token_state: Optional[dict] = None

    name: ClassVar[str] = TURN_END


@dataclass
class RuntimeDurabilityChangedEvent:
    """A managed runtime's recoverable revision fell behind or caught up."""

    runtime_id: str = ""
    runtime_kind: str = ""
    alias: str = "default"
    state: str = "not_configured"
    current_revision: int = 0
    recoverable_revision: int = 0
    detail: str = ""

    name: ClassVar[str] = RUNTIME_DURABILITY_CHANGED


@dataclass
class MessageAppendedEvent:
    """A message was appended to the stored history."""

    message: "Message" = None  # type: ignore[assignment]

    name: ClassVar[str] = MESSAGE_APPENDED


@dataclass
class OutputCandidateReceivedEvent(_DurableFact):
    """A terminal output candidate entered the run-scoped output engine."""

    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    representation: str = ""
    raw: Any = None
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_CANDIDATE_RECEIVED
    type: ClassVar[str] = OUTPUT_CANDIDATE_RECEIVED


@dataclass
class OutputValidationRejectedEvent(_DurableFact):
    """A candidate failed its output contract and was not accepted."""

    candidate_id: str = ""
    contract_id: str = ""
    issues: List[dict] = field(default_factory=list)
    correction_attempt: int = 0
    corrections_remaining: int = 0
    correction_allowed: bool = False
    validator_provenance: List[dict] = field(default_factory=list)
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_VALIDATION_REJECTED
    type: ClassVar[str] = OUTPUT_VALIDATION_REJECTED


@dataclass
class OutputAcceptedEvent(_DurableFact):
    """A candidate decoded and validated successfully, before durable commit."""

    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    value: Any = None
    correction_attempts: int = 0
    validator_provenance: List[dict] = field(default_factory=list)
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_ACCEPTED
    type: ClassVar[str] = OUTPUT_ACCEPTED


@dataclass
class OutputCommitStartedEvent(_DurableFact):
    """Durable commit began for an already accepted output."""

    candidate_id: str = ""
    contract_id: str = ""
    run_id: str = ""
    run_kind: str = "agent"
    fencing_token: int = 0

    name: ClassVar[str] = OUTPUT_COMMIT_STARTED
    type: ClassVar[str] = OUTPUT_COMMIT_STARTED


@dataclass
class OutputMigratedEvent(_DurableFact):
    """An explicit migration produced a candidate for the current contract."""

    candidate_id: str = ""
    source_contract_id: str = ""
    target_contract_id: str = ""
    target_schema_fingerprint: str = ""
    value: Any = None
    steps: List[dict] = field(default_factory=list)
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_MIGRATED
    type: ClassVar[str] = OUTPUT_MIGRATED


@dataclass
class OutputCommittedEvent(_DurableFact):
    """The accepted output and its transcript crossed the durability barrier."""

    candidate_id: str = ""
    contract_id: str = ""
    schema_fingerprint: str = ""
    value: Any = None
    correction_attempts: int = 0
    validator_provenance: List[dict] = field(default_factory=list)
    run_id: str = ""
    run_kind: str = "agent"
    fencing_token: int = 0

    name: ClassVar[str] = OUTPUT_COMMITTED
    type: ClassVar[str] = OUTPUT_COMMITTED


@dataclass
class OutputPublicationQueuedEvent(_DurableFact):
    """A committed output entered the durable publication outbox."""

    publication_id: str = ""
    candidate_id: str = ""
    contract_id: str = ""
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_PUBLICATION_QUEUED
    type: ClassVar[str] = OUTPUT_PUBLICATION_QUEUED


@dataclass
class OutputPublishedEvent(_DurableFact):
    """A committed output crossed the Role's outward publication boundary."""

    candidate_id: str = ""
    contract_id: str = ""
    publication_id: str = ""
    run_id: str = ""
    run_kind: str = "agent"

    name: ClassVar[str] = OUTPUT_PUBLISHED
    type: ClassVar[str] = OUTPUT_PUBLISHED


@dataclass
class LLMStreamDeltaEvent:
    """One streamed token (or chunk) from the LLM client."""

    token: str = ""
    model_call_id: str = ""
    attempt_id: str = ""
    sequence: int = 0
    provisional: bool = False

    name: ClassVar[str] = LLM_STREAM_DELTA


@dataclass
class LLMStreamCommittedEvent:
    """One accepted attempt's buffered deltas became visible output."""

    model_call_id: str = ""
    attempt_id: str = ""
    chunk_count: int = 0

    name: ClassVar[str] = LLM_STREAM_COMMITTED


@dataclass
class LLMStreamDiscardedEvent:
    """One failed attempt's provisional deltas were permanently rejected."""

    model_call_id: str = ""
    attempt_id: str = ""
    chunk_count: int = 0
    reason: str = "attempt_failed"

    name: ClassVar[str] = LLM_STREAM_DISCARDED


@dataclass
class LLMStreamInterruptedEvent:
    """A cancelled logical call ended with an uncommitted attempt stream."""

    model_call_id: str = ""
    attempt_id: str = ""
    chunk_count: int = 0
    reason: str = "cancelled"

    name: ClassVar[str] = LLM_STREAM_INTERRUPTED


@dataclass
class OutputSnapshotEvent:
    """A provisional structured value parsed from an in-flight LLM stream."""

    run_id: str = ""
    revision: int = 0
    schema_fingerprint: str = ""
    value: Any = None

    name: ClassVar[str] = OUTPUT_SNAPSHOT


@dataclass
class OutputSnapshotInvalidatedEvent:
    """A previously emitted provisional revision is no longer valid."""

    run_id: str = ""
    revision: int = 0
    reason: str = "stream_changed"

    name: ClassVar[str] = OUTPUT_SNAPSHOT_INVALIDATED


@dataclass
class RunLeaseEvent:
    """Low-frequency ownership lifecycle telemetry; never a truth source."""

    phase: str = ""
    run_id: str = ""
    owner_id: str = ""
    fencing_token: int = 0
    expires_at: float = 0.0
    reason: str = ""

    name: ClassVar[str] = RUN_LEASE


@dataclass
class LLMStreamEndEvent:
    """The current LLM stream finished (turn boundary for the renderer)."""

    name: ClassVar[str] = LLM_STREAM_END


@dataclass
class ModelCallPlannedEvent:
    model_call_id: str = ""
    routing_decision_id: str = ""
    plan_id: str = ""
    route_id: str = ""
    config_revision: str = ""
    policy_id: str = ""
    resume_generation: int = 0
    endpoint_ids: List[str] = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    trace_id: str = ""

    name: ClassVar[str] = MODEL_CALL_PLANNED


@dataclass
class ModelAttemptAdmissionRejectedEvent:
    model_call_id: str = ""
    resume_generation: int = 0
    endpoint_id: str = ""
    credential_slot_id: str = ""
    gate: str = ""
    reason: str = ""
    trace_id: str = ""

    name: ClassVar[str] = MODEL_ATTEMPT_ADMISSION_REJECTED


@dataclass
class ModelAttemptStartedEvent:
    model_call_id: str = ""
    attempt_id: str = ""
    ordinal: int = 0
    resume_generation: int = 0
    endpoint_id: str = ""
    credential_slot_id: str = ""
    model: str = ""
    provider: str = ""
    input: Any = None
    timeout_seconds: float = 0.0
    parent_span_id: Optional[str] = None
    trace_id: str = ""

    name: ClassVar[str] = MODEL_ATTEMPT_STARTED


@dataclass
class ModelAttemptFinishedEvent:
    model_call_id: str = ""
    attempt_id: str = ""
    ordinal: int = 0
    resume_generation: int = 0
    endpoint_id: str = ""
    state: str = ""
    failure_reason: str = ""
    latency_ms: float = 0.0
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    output: Any = None
    trace_id: str = ""

    name: ClassVar[str] = MODEL_ATTEMPT_FINISHED


@dataclass
class ModelFallbackSelectedEvent:
    model_call_id: str = ""
    resume_generation: int = 0
    from_endpoint_id: str = ""
    to_endpoint_id: str = ""
    reason: str = ""
    wire_attempts_used: int = 0
    trace_id: str = ""

    name: ClassVar[str] = MODEL_FALLBACK_SELECTED


@dataclass
class ModelCallFinishedEvent:
    model_call_id: str = ""
    state: str = ""
    selected_endpoint_id: str = ""
    wire_attempts: int = 0
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    summary: dict = field(default_factory=dict)
    trace_id: str = ""

    name: ClassVar[str] = MODEL_CALL_FINISHED


@dataclass
class RoutingDecisionEvent(_DurableFact):
    """A guarded semantic route decision committed before model execution."""

    decision: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    name: ClassVar[str] = ROUTING_DECISION
    type: ClassVar[str] = ROUTING_DECISION


@dataclass
class ContextCompactedEvent:
    """A compaction committed a new model-context projection."""

    model_context_messages: List["Message"] = field(default_factory=list)
    source_message_ids: List[str] = field(default_factory=list)
    summary: str = ""
    strategy: str = ""
    trigger: str = "auto"

    name: ClassVar[str] = CONTEXT_COMPACTED


@dataclass
class HistoryEditedEvent:
    """A user removed messages from the logical transcript and model context."""

    remaining_messages: List["Message"] = field(default_factory=list)
    removed_message_ids: List[str] = field(default_factory=list)
    clear_all: bool = False
    reason: Literal["delete", "clear"] = "delete"

    name: ClassVar[str] = HISTORY_EDITED


@dataclass
class FileChangedEvent:
    """An exact, externally attributed file-version transition."""

    path: str
    change_type: FileChangeKind
    prior_version: FileVersion
    version: FileVersion
    attribution: FileChangeAttribution = FileChangeAttribution.EXTERNAL

    name: ClassVar[str] = FILE_CHANGED


@dataclass
class FileMutatedEvent:
    """A tool just successfully wrote/created/deleted a file on disk.

    Emitted by the :class:`ToolExecutor` right after a filesystem-mutating tool
    (Write/Edit/...) succeeds, carrying the resolved path. Purely
    an observation for derived services. It is not file-change attribution:
    only File Operations' exact durable commit facts establish a managed
    transition. Distinct from :class:`FileChangedEvent`, which the watcher emits
    for externally attributed transitions.
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
    """A resource's :class:`~mote.runtime.resilience.CircuitBreaker` changed state.

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
    :class:`~mote.product.cli.contracts.view.events.ActivityStarted` ViewEvent.

    ``scope`` identifies the activity (its :class:`~mote.runtime.events.scope.
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

    The orchestration layer (control / residency) runs outside per-turn Role
    execution, so it owns a persistent TelemetryRuntime for these milestones:
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
    (:mod:`~mote.runtime.events.trace`). The instance field is ``label`` (the
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
    (:class:`~mote.runtime.durable.think_journal.ThinkJournal` think steps, durable
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
# Prompt observation fact. PromptPolicy has already completed before this event
# is emitted, so subscribers can observe only the admitted, secret-safe view.
# ---------------------------------------------------------------------------


@dataclass
class UserPromptSubmitEvent:
    """Safe observation that a user prompt entered this turn."""

    prompt: str = ""

    name: ClassVar[str] = USER_PROMPT_SUBMIT


@dataclass
class PromptRejectedEvent(_DurableFact):
    """Secret-safe fact that PromptPolicy denied admission before a turn."""

    prompt: str = ""
    reason: str = ""
    terminate: bool = False

    name: ClassVar[str] = PROMPT_REJECTED
    type: ClassVar[str] = PROMPT_REJECTED


@dataclass
class ToolInvocationStartedEvent:
    """Observation emitted at the irreversible tool invocation boundary."""

    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_use_id: Optional[str] = None
    scope: tuple = ()

    name: ClassVar[str] = TOOL_INVOCATION_STARTED


@dataclass
class ToolCallFinishedEvent:
    """Safe observation of a succeeded, failed, or rejected tool call."""

    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_response: Any = None
    tool_use_id: Optional[str] = None
    outcome: Literal["succeeded", "failed", "rejected"] = "succeeded"
    #: Structured failure record on a non-success result (``ErrorReport``), mirrored
    #: from the ``ToolResult``; ``None`` on success or for a legacy output-only fail.
    error: Optional["ErrorReport"] = None
    #: Structured media the tool produced (``list[ToolMedia]``: image/pdf artifacts),
    #: mirrored from the ``ToolResult`` so the view layer folds a media block from the
    #: fact instead of sniffing ``tool_response`` text / reverse-engineering a path.
    media: list = field(default_factory=list)
    #: Durable non-media products emitted by the tool. References are opaque;
    #: consumers must not reinterpret them as filesystem paths or media payloads.
    artifacts: list["ArtifactRef"] = field(default_factory=list)
    #: Structured file modifications the tool made (``list[FileChange]``: path/old/new),
    #: mirrored from the ``ToolResult`` so the view layer renders the change from the
    #: fact — side-by-side on a rich host, a synthesized coloured diff on a text host —
    #: instead of sniffing ``tool_response`` text for a diff shape.
    file_changes: list = field(default_factory=list)
    #: Execution lineage (``ScopePath``) this call ran under. ``()`` = top level.
    scope: tuple = ()

    name: ClassVar[str] = TOOL_CALL_FINISHED


@dataclass
class PostCompactEvent:
    """The model-context projection was compacted; summary is optional."""

    trigger: str = "auto"
    summary: str = ""

    name: ClassVar[str] = POST_COMPACT


# The single definition of "the active model context was structurally rebuilt,
# so incremental context sources must re-project their state." Compaction keeps
# the logical transcript intact but changes the model projection; a user edit
# removes stable message IDs from both projections. In either case, a source that
# caches what it has already injected into the model context must reset.
MODEL_CONTEXT_REBUILT_EVENTS: tuple[type, ...] = (
    PostCompactEvent,
    HistoryEditedEvent,
)


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
    "LLM_STREAM_COMMITTED",
    "LLM_STREAM_DISCARDED",
    "LLM_STREAM_INTERRUPTED",
    "LLM_STREAM_END",
    "OUTPUT_SNAPSHOT",
    "OUTPUT_SNAPSHOT_INVALIDATED",
    "RUN_LEASE",
    "RUNTIME_DURABILITY_CHANGED",
    "CONTEXT_COMPACTED",
    "HISTORY_EDITED",
    "USER_PROMPT_SUBMIT",
    "PROMPT_REJECTED",
    "TOOL_INVOCATION_STARTED",
    "TOOL_CALL_FINISHED",
    "POST_COMPACT",
    "FILE_CHANGED",
    "FILE_MUTATED",
    "TOOLS_CHANGED",
    "DIAGNOSTICS",
    "RECOVERY",
    "ROUTING_DECISION",
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
    # observation events
    "SessionStartEvent",
    "SessionEndEvent",
    "TurnStartEvent",
    "TurnEndEvent",
    "RuntimeDurabilityChangedEvent",
    "MessageAppendedEvent",
    "LLMStreamDeltaEvent",
    "LLMStreamCommittedEvent",
    "LLMStreamDiscardedEvent",
    "LLMStreamInterruptedEvent",
    "LLMStreamEndEvent",
    "OutputSnapshotEvent",
    "OutputSnapshotInvalidatedEvent",
    "RunLeaseEvent",
    "OutputMigratedEvent",
    "ContextCompactedEvent",
    "HistoryEditedEvent",
    "FileChangedEvent",
    "FileMutatedEvent",
    "ToolsChangedEvent",
    "DiagnosticsEvent",
    "RecoveryEvent",
    "RoutingDecisionEvent",
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
    "PromptRejectedEvent",
    # control events
    "UserPromptSubmitEvent",
    "ToolInvocationStartedEvent",
    "ToolCallFinishedEvent",
    "PostCompactEvent",
    "MODEL_CONTEXT_REBUILT_EVENTS",
    # union
    "AgentEvent",
]
