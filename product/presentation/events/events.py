#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``ViewEvent`` — the human protocol (one of the two 窄腰).

Stable, coarse-grained, **pure-presentation** open tagged-union. Each event
carries *display intent* (what the human should see) — never *delivery*
(how it gets there). Adding a new ``ViewEvent`` never breaks an existing
consumer; consumers switch on ``kind`` and ignore what they don't know.

This is a *union*, not a *hierarchy* (ARCHITECTURE §2.2.1): the only shared
contract is the ``kind`` discriminator (aligned with ``AgentEvent.name``). There
is deliberately **no** common base with the machine protocol's
``ServerNotification`` — a shared base would become a coupling backdoor that
smuggles human-only fields into the machine contract.

Everything the old ``render.py`` recomputed per-frontend — which arg is the
tool's *headline*, which arg is the *body* and its syntax *lexer*, whether a
tool result is a *failure*, the one-line *summary* — is **already decided** by
the projector and lands here as neutral, pre-computed data (``title`` / ``lexer``
/ ``summary`` are *fields*, not logic each consumer re-derives).

This lives in the shared ``contracts`` layer (not a single host) because the human
contract is shared across every host that renders for a person — terminal / Web
/ IM all consume the same ``ViewEvent`` union.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, List, Optional

from pydantic import BaseModel

from mote.contracts.activity import ActivityKind, ActivityNodeState, ActivityOutcome, ActivityTopology
from mote.contracts.artifact import ArtifactRef
from mote.contracts.events.scope import ScopePath
from mote.contracts.tool.identity import ToolInvocationIdentity

# Discriminator constants (aligned with ``AgentEvent.name`` where 1:1).
MESSAGE_BLOCK_STARTED = "message_block_started"
MESSAGE_BLOCK_DELTA = "message_block_delta"
MESSAGE_BLOCK_COMPLETED = "message_block_completed"
ATTEMPT_STREAM_COMMITTED = "attempt_stream_committed"
ATTEMPT_STREAM_DISCARDED = "attempt_stream_discarded"
ATTEMPT_STREAM_INTERRUPTED = "attempt_stream_interrupted"
REASONING_DELTA = "reasoning_delta"
TOOL_CALL_STARTED = "tool_call_started"
TOOL_CALL_COMPLETED = "tool_call_completed"
MEDIA_BLOCK = "media_block"
ARTIFACT_BLOCK = "artifact_block"
FILE_DIFF_BLOCK = "file_diff_block"
TASK_PROGRESS = "task_progress"
NOTICE = "notice"
ERROR_RAISED = "error_raised"
QUESTION_ASKED = "question_asked"
APPROVAL_REQUESTED = "approval_requested"
USAGE_UPDATED = "usage_updated"
SESSION_LIST_SHOWN = "session_list_shown"
RETRY_STATUS = "retry_status"
RUNTIME_DURABILITY_STATUS = "runtime_durability_status"
TRANSCRIPT_CLEARED = "transcript_cleared"
SYSTEM_REMINDER = "system_reminder"
CONVERSATION_COMPACTED = "conversation_compacted"
ACTIVITY_STARTED = "activity_started"
ACTIVITY_COMPLETED = "activity_completed"
OUTPUT_SNAPSHOT = "output_snapshot"
OUTPUT_SNAPSHOT_INVALIDATED = "output_snapshot_invalidated"
OUTPUT_COMMITTED = "output_committed"
ASYNC_WORK_OBSERVED = "async_work_observed"

# ``ToolCallCompleted.result_kind`` values — the neutral signal telling a consumer
# *which* renderer a tool result wants. The projector decides this once; consumers
# switch on it (a diff gets +/- coloring, a table gets columns) instead of each
# host re-sniffing the text. Media results ride on a companion ``MediaBlock``.
RESULT_KIND_PLAIN = "plain"
RESULT_KIND_DIFF = "diff"
RESULT_KIND_TABLE = "table"
RESULT_KIND_MEDIA = "media"


class ViewEvent(BaseModel):
    """Base of the human-protocol tagged union.

    ``kind`` is a ``ClassVar`` discriminator — it is part of the *type*, not a
    per-instance field, so it never bloats serialized payloads while still
    letting consumers dispatch on ``type(ev).kind`` or ``ev.kind``.
    """

    kind: ClassVar[str] = "view_event"

    # Execution lineage (a ``ScopePath`` — tuple of ``ScopeRef``) this event
    # belongs to, carried forward from the machine event by the projector.
    # ``()`` = top level (today's exact behavior). A consumer that groups by
    # activity reads this; a flat consumer ignores it. Non-breaking by
    # construction: it defaults, so every existing ViewEvent constructs unchanged.
    scope: ScopePath = ()


class MessageBlockStarted(ViewEvent):
    """An assistant/human message block began (a streaming region opens)."""

    kind: ClassVar[str] = MESSAGE_BLOCK_STARTED
    role: str = "assistant"


class MessageBlockDelta(ViewEvent):
    """One streamed token/chunk of the open message block."""

    kind: ClassVar[str] = MESSAGE_BLOCK_DELTA
    text: str = ""
    model_call_id: str = ""
    attempt_id: str = ""
    sequence: int = 0
    provisional: bool = False


class AttemptStreamCommitted(ViewEvent):
    kind: ClassVar[str] = ATTEMPT_STREAM_COMMITTED
    model_call_id: str = ""
    attempt_id: str = ""
    chunk_count: int = 0


class AttemptStreamDiscarded(ViewEvent):
    kind: ClassVar[str] = ATTEMPT_STREAM_DISCARDED
    model_call_id: str = ""
    attempt_id: str = ""
    chunk_count: int = 0
    reason: str = ""


class AttemptStreamInterrupted(ViewEvent):
    kind: ClassVar[str] = ATTEMPT_STREAM_INTERRUPTED
    model_call_id: str = ""
    attempt_id: str = ""
    chunk_count: int = 0
    reason: str = ""


class MessageBlockCompleted(ViewEvent):
    """A full message block (complete markdown).

    ``streamed`` records whether deltas for this block were already emitted —
    a streaming consumer uses it to *finalize* its live region instead of
    re-printing, while a non-streaming consumer renders the markdown fresh.
    """

    kind: ClassVar[str] = MESSAGE_BLOCK_COMPLETED
    role: str = "assistant"
    markdown: str = ""
    streamed: bool = False
    # Truncation is a *semantic* property of the content (human-readable "this was
    # folded"), distinct from the DeliveryManager's physical chunking (§7.3). When
    # ``content_truncated`` a consumer shows a "[folded]" affordance; ``full_ref``
    # points at the complete body (disk path / URL) so it can offer "see full".
    content_truncated: bool = False
    hidden_lines: int = 0
    full_ref: Optional[str] = None
    # The stored ``Message.id`` this block was rendered from (human turns only —
    # the driver threads it so a UserMessageRow can be mapped back to the exact
    # history message when a react-unit is deleted). ``None`` for assistant blocks
    # (which the projector emits and which are not delete anchors).
    message_id: Optional[str] = None


class ReasoningDelta(ViewEvent):
    """One streamed token of the model's *reasoning* (think) stream."""

    kind: ClassVar[str] = REASONING_DELTA
    text: str = ""


class OutputSnapshot(ViewEvent):
    kind: ClassVar[str] = OUTPUT_SNAPSHOT
    run_id: str = ""
    revision: int = 0
    schema_fingerprint: str = ""
    value: object = None


class OutputSnapshotInvalidated(ViewEvent):
    kind: ClassVar[str] = OUTPUT_SNAPSHOT_INVALIDATED
    run_id: str = ""
    revision: int = 0
    reason: str = ""


class OutputCommitted(ViewEvent):
    kind: ClassVar[str] = OUTPUT_COMMITTED
    run_id: str = ""
    run_kind: str = "agent"
    contract_id: str = ""
    schema_fingerprint: str = ""
    value: object = None


class ToolCallStarted(ViewEvent):
    """A tool is about to run — title/headline/body/lexer already derived.

    The projector has already chosen *which* arg is the headline, *which* arg
    is the body, and *which* syntax lexer highlights it. The consumer just
    renders these neutral fields.
    """

    kind: ClassVar[str] = TOOL_CALL_STARTED
    identity: ToolInvocationIdentity
    tool_name: str = ""
    title: str = ""
    headline: str = ""
    body: Optional[str] = None
    lexer: Optional[str] = None


class ToolCallCompleted(ViewEvent):
    """A tool finished — success already judged, summary already extracted.

    ``result_kind`` (one of ``RESULT_KIND_*``) tells the consumer how to render
    the (optional) ``detail`` body: a ``diff`` gets +/- highlighting, a ``table``
    gets columns (``detail`` is TSV), ``plain`` needs no body (``summary`` alone).
    ``media`` marks that the real content rides on a companion ``MediaBlock``.
    Adding these fields is non-breaking: all default, so existing projector
    construction sites and consumers are unaffected until they opt in.
    """

    kind: ClassVar[str] = TOOL_CALL_COMPLETED
    identity: ToolInvocationIdentity
    tool_name: str = ""
    ok: bool = True
    summary: str = ""
    result_kind: str = RESULT_KIND_PLAIN
    detail: Optional[str] = None
    lexer: Optional[str] = None
    # Truncation semantics (see MessageBlockCompleted): ``full_ref`` dovetails with
    # the framework's ``tool_result_limit`` on-disk ``.tool_results/{id}.txt`` — it
    # is that persisted path, not a physical wire-chunk.
    content_truncated: bool = False
    full_ref: Optional[str] = None
    # How many *lines* the projector dropped from the rendered detail (0 = none /
    # unknown). Lets a consumer show a precise "+N 行已折叠" fold hint (the
    # "… +N lines" form) instead of a bare "folded" note. Distinct from ``full_ref``
    # (a hard truncation persisted to disk); both can coexist.
    hidden_lines: int = 0
    # Structured failure facts (from the executor's ErrorReport; empty on success
    # or when the event carries no structured error). Flat scalars only — the
    # contract layer never imports the exception type (leaf discipline).
    error_type: str = ""  # exception class name, e.g. "PermissionError"
    error_code: str = ""  # stable ErrorCode string, e.g. "tool.permission_denied"
    retryable: bool = False
    recovery: str = ""  # one-line remediation hint, human-facing


@dataclass(frozen=True, slots=True)
class UserMediaIdentity:
    """Stable identity for one media item attached to a stored user message."""

    message_id: str
    ordinal: int

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("UserMediaIdentity.message_id must not be empty")
        if isinstance(self.ordinal, bool) or self.ordinal < 1:
            raise ValueError("UserMediaIdentity.ordinal must be positive")


class MediaBlock(ViewEvent):
    """A media artifact a tool produced (image / pdf / ...) — first-class content.

    The projector emits this alongside a ``ToolCallCompleted(result_kind=media)``
    so a media-capable host (Web ``<img>``, IM upload) renders the artifact while
    a text-only host degrades to printing ``alt`` (or ``ref``). ``ref`` is the
    locator (path / URL / data-uri); *how* it's delivered is the consumer's call,
    never encoded here (display intent, not delivery — §2.2).
    """

    kind: ClassVar[str] = MEDIA_BLOCK
    identity: ToolInvocationIdentity | UserMediaIdentity
    media_kind: str = "image"  # image | pdf | audio (reserved)
    ref: str = ""  # path / URL / data-uri
    mime: Optional[str] = None
    artifact: Optional[ArtifactRef] = None
    alt: str = ""  # degrade text when the host has no media capability


class ArtifactBlock(ViewEvent):
    """A durable non-media tool product identified by an opaque ArtifactRef."""

    kind: ClassVar[str] = ARTIFACT_BLOCK
    identity: ToolInvocationIdentity
    artifact: ArtifactRef


class FileDiffBlock(ViewEvent):
    """A file change a tool made (e.g. Edit), as the *structured fact*.

    The change-content counterpart to ``MediaBlock``: the projector emits this
    alongside a ``ToolCallCompleted(result_kind=diff)`` so a host renders the
    change from ``old``/``new`` — the two full contents — rather than sniffing the
    output text for a diff shape. This is deliberately **not** a pre-formatted diff
    string: a rich host (Web) drives an interactive side-by-side review from
    old/new, while a text host synthesizes a coloured unified diff (its private
    display choice). ``old==""`` marks a creation, ``new==""`` a deletion.
    """

    kind: ClassVar[str] = FILE_DIFF_BLOCK
    identity: ToolInvocationIdentity
    path: str = ""  # absolute path of the changed file (dest after a move)
    old: str = ""  # full content before ("" when created)
    new: str = ""  # full content after ("" when deleted)


class TaskProgress(ViewEvent):
    """A background task reported a progress line."""

    kind: ClassVar[str] = TASK_PROGRESS
    stage: str = ""
    status: str = ""
    detail: str = ""


class AsyncWorkObserved(ViewEvent):
    """A live immutable projection; local variants are never replayed."""

    kind: ClassVar[str] = ASYNC_WORK_OBSERVED
    observation_json: str


class Notice(ViewEvent):
    """A system notice (^C hints, command output, lifecycle messages)."""

    kind: ClassVar[str] = NOTICE
    text: str = ""
    level: str = "info"  # info | warning | success


class ErrorRaised(ViewEvent):
    """A turn surfaced an error to the user."""

    kind: ClassVar[str] = ERROR_RAISED
    text: str = ""


class QuestionAsked(ViewEvent):
    """The agent asked the user a question (AskUserQuestion / ask_user)."""

    kind: ClassVar[str] = QUESTION_ASKED
    question: str = ""
    options: Optional[List[str]] = None


class ApprovalRequested(ViewEvent):
    """A gated action awaits the human's approval (permission round-trip).

    Distinct from ``QuestionAsked`` (free-form Q&A): approval needs a *structured
    decision back*. This ViewEvent carries the display side (what the human sees);
    the decision flows back **up** through :meth:`InputPort.decide_approval` as an
    :class:`ApprovalDecision` — ViewEvent is one-way downstream display intent, so
    the return value is not a ViewEvent (symmetric with ``ask`` returning ``str``).

    ``approval_id`` correlates the request to its decision. ``risk`` is a neutral
    band (``low``/``medium``/``high``) the projector already judged.
    """

    kind: ClassVar[str] = APPROVAL_REQUESTED
    tool_name: str = ""
    action: str = ""  # human-readable action ("Run: rm -rf build/")
    args_preview: str = ""  # pre-formatted arg preview (projector computed it)
    risk: str = "medium"  # low | medium | high
    approval_id: str = ""
    lexer: Optional[str] = None


@dataclass(frozen=True)
class ApprovalDecision:
    """The human's answer to an :class:`ApprovalRequested` — an inbound round-trip.

    NOT a ViewEvent: it flows back up through :meth:`InputPort.decide_approval`,
    the same way ``ask`` returns a ``str``. ``edited_args`` is reserved for
    "approve but with these changed arguments".
    """

    approval_id: str
    outcome: str = "reject"  # accept | reject | always_allow | always_deny
    edited_args: Optional[dict[str, Any]] = None


class UsageUpdated(ViewEvent):
    """Token / cost / context usage the human's status line shows.

    All figures are **pre-computed** by the projector — ``context_pct`` is already
    the 0-1 ratio, so a consumer never divides ``context_used`` by
    ``context_window`` itself. A host with no status line simply eats this event.
    """

    kind: ClassVar[str] = USAGE_UPDATED
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None
    context_used: Optional[int] = None  # tokens of context consumed
    context_window: Optional[int] = None  # window ceiling
    context_pct: Optional[float] = None  # 0-1, precomputed
    model: Optional[str] = None


class SessionListItem(BaseModel):
    """One row of a :class:`SessionListShown` — a resumable session, pre-formatted.

    NOT a ViewEvent (it never stands alone); it is the neutral element the
    projector/driver fills so a media-capable host can render a *clickable* list
    (Web/IM) while a terminal renders a numbered table. ``index`` mirrors the
    driver's cached order so ``/resume <index>`` stays valid.
    """

    session_id: str = ""
    label: str = ""
    updated_at: Optional[str] = None
    preview: str = ""
    index: int = 0


class SessionListShown(ViewEvent):
    """The resumable-session list, as a structured event (not a ``Notice`` blob).

    The driver's ``list_resumable_sessions`` used to render only as free-text
    through ``Notice`` — unrenderable as a list on Web/IM. This carries the rows
    as data so every host presents them natively; a text host still degrades to a
    numbered list, a structured host serializes the array verbatim.
    """

    kind: ClassVar[str] = SESSION_LIST_SHOWN
    items: List[SessionListItem] = []
    title: str = "Sessions"


class RetryStatus(ViewEvent):
    """A transient LLM-retry countdown — a *temporary* display, not a transcript entry.

    A self-updating "Retrying in Ns… (attempt X/Y)" line: the
    consumer shows it in an erasable region (terminal ``Live(transient=True)`` /
    Textual ``StatusBar``) and **clears it the moment any other event arrives**
    (a stream token = retry succeeded; a final ``ErrorRaised`` = budget exhausted).
    There is deliberately no explicit "clear" event — "temporary" is the contract.
    """

    kind: ClassVar[str] = RETRY_STATUS
    attempt: int = 0
    max_attempts: int = 0
    delay_ms: float = 0.0
    error: str = ""
    error_type: str = ""


class RuntimeDurabilityStatus(ViewEvent):
    """Transient managed-runtime checkpoint lag or recovery status."""

    kind: ClassVar[str] = RUNTIME_DURABILITY_STATUS
    runtime_id: str = ""
    runtime_kind: str = ""
    alias: str = "default"
    state: str = "not_configured"
    current_revision: int = 0
    recoverable_revision: int = 0
    detail: str = ""


class TranscriptCleared(ViewEvent):
    """The conversation was cleared (``/clear``) — wipe the rendered transcript.

    A display-intent signal: the agent's stored history has already been reset
    up in the driver; this tells every consumer to drop what it has shown so the
    human sees a fresh screen. A host with no persistent transcript (webhook,
    JSON-lines) simply eats it.
    """

    kind: ClassVar[str] = TRANSCRIPT_CLEARED


class SystemReminder(ViewEvent):
    """A framework-injected ``<system-reminder>`` block, surfaced to the human.

    mote's turn-context sources (git snapshot, token-pressure, changed-files,
    skill/tool listings, compaction notice, ...) inject a ``<system-reminder>``
    envelope into the model's prompt each turn. That context reaches the LLM but
    was invisible to the human — this event makes it visible. ``text`` is a
    **pre-summarized** one-liner (the projector already stripped the tags and
    condensed each block to its ``# heading``), so a consumer renders it as a
    single dim, unobtrusive row rather than dumping the raw injected prose.
    """

    kind: ClassVar[str] = SYSTEM_REMINDER
    text: str = ""


class ConversationCompacted(ViewEvent):
    """The conversation history was compacted — a *boundary* marker in the transcript.

    mote's ``CompactionEngine`` rebuilds history when the context fills up,
    condensing earlier turns into a ``summary`` (the first message of the new
    history). This event surfaces that boundary so the human sees *why* the
    transcript appears to jump — mirroring the dim
    "✻ Conversation compacted" line. ``summary`` carries the model-generated
    recap (a media-capable host may reveal it on demand); ``message_count`` is
    the size of the rebuilt history. A terminal renders only a dim marker line.
    """

    kind: ClassVar[str] = CONVERSATION_COMPACTED
    summary: str = ""
    message_count: int = 0


class ActivityStarted(ViewEvent):
    """A nested orchestration (a ``run_graph`` graph, a sub-agent, a background
    task) began — carries the *topology* the consumer draws before any step runs.

    The activity is identified by its ``scope`` (inherited from the base): the
    reducer keys an open activity by that ``ScopePath``, and every later scoped
    ``TaskProgress`` / tool event / ``ActivityCompleted`` with the same head
    updates the same subtree. ``topology`` is the canonical neutral activity
    contract; the L4
    ``activity_topology`` renderer turns it into a tree/graph. It is display-only:
    a live "which node is running" overlay rides on later ``TaskProgress`` events.
    """

    kind: ClassVar[str] = ACTIVITY_STARTED
    activity_kind: ActivityKind = ActivityKind.GRAPH
    label: str = ""
    topology: ActivityTopology | None = None


class ActivityCompleted(ViewEvent):
    """A nested orchestration finished — carries a *self-sufficient* outcome tree.

    Self-sufficiency is the invariant (mirrors ``ToolCallCompleted`` carrying its
    own truncation): ``node_states`` and ``outcome`` fully describe the terminal
    render read straight off the graph's terminal state, so a replayed / resumed
    transcript (which has only this event, never the live ``TaskProgress`` stream)
    renders the full outcome. ``node_states`` and ``outcome`` retain their
    canonical typed contracts; ``summary`` is a human one-liner. The L4
    ``activity_outcome`` renderer turns them into the final ✓/⊘/✗ tree.
    """

    kind: ClassVar[str] = ACTIVITY_COMPLETED
    outcome: ActivityOutcome = ActivityOutcome.SUCCESS
    node_states: tuple[ActivityNodeState, ...] = ()
    summary: str = ""


__all__ = [
    "ViewEvent",
    "MessageBlockStarted",
    "MessageBlockDelta",
    "MessageBlockCompleted",
    "AttemptStreamCommitted",
    "AttemptStreamDiscarded",
    "AttemptStreamInterrupted",
    "ReasoningDelta",
    "OutputSnapshot",
    "OutputSnapshotInvalidated",
    "OutputCommitted",
    "ToolCallStarted",
    "ToolCallCompleted",
    "MediaBlock",
    "UserMediaIdentity",
    "ArtifactBlock",
    "FileDiffBlock",
    "TaskProgress",
    "AsyncWorkObserved",
    "Notice",
    "ErrorRaised",
    "QuestionAsked",
    "ApprovalRequested",
    "ApprovalDecision",
    "UsageUpdated",
    "SessionListItem",
    "SessionListShown",
    "RetryStatus",
    "RuntimeDurabilityStatus",
    "TranscriptCleared",
    "SystemReminder",
    "ConversationCompacted",
    "ActivityStarted",
    "ActivityCompleted",
    "MESSAGE_BLOCK_STARTED",
    "MESSAGE_BLOCK_DELTA",
    "MESSAGE_BLOCK_COMPLETED",
    "ATTEMPT_STREAM_COMMITTED",
    "ATTEMPT_STREAM_DISCARDED",
    "ATTEMPT_STREAM_INTERRUPTED",
    "REASONING_DELTA",
    "OUTPUT_SNAPSHOT",
    "OUTPUT_SNAPSHOT_INVALIDATED",
    "OUTPUT_COMMITTED",
    "ASYNC_WORK_OBSERVED",
    "TOOL_CALL_STARTED",
    "TOOL_CALL_COMPLETED",
    "MEDIA_BLOCK",
    "ARTIFACT_BLOCK",
    "FILE_DIFF_BLOCK",
    "TASK_PROGRESS",
    "NOTICE",
    "ERROR_RAISED",
    "QUESTION_ASKED",
    "APPROVAL_REQUESTED",
    "USAGE_UPDATED",
    "SESSION_LIST_SHOWN",
    "RETRY_STATUS",
    "RUNTIME_DURABILITY_STATUS",
    "TRANSCRIPT_CLEARED",
    "SYSTEM_REMINDER",
    "CONVERSATION_COMPACTED",
    "ACTIVITY_STARTED",
    "ACTIVITY_COMPLETED",
    "RESULT_KIND_PLAIN",
    "RESULT_KIND_DIFF",
    "RESULT_KIND_TABLE",
    "RESULT_KIND_MEDIA",
]
