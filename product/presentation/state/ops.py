#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TranscriptOp`` — the neutral op vocabulary the reducer emits.

A :class:`~mote.product.presentation.state.reducer.TranscriptReducer` folds the
``ViewEvent`` stream into a stream of these ops: *what happened*, fully
semantic, with **zero host awareness**. Each host then implements a thin
:class:`~mote.product.presentation.state.surface.RenderSurface` — one method per
op — and lands each op with its own primitives (a scrolling terminal prints a
line; a Textual app mutates a widget). This is the single state machine both
rich hosts share, so "when to merge a tool run / when to clear a transient /
when a turn ends" is decided **once**.

These are frozen ``dataclass`` (not pydantic): they live only on the in-process
hot path between reducer and surface — never serialized — so a plain dataclass
is the leaner tool. Each op carries a ``ClassVar[str] kind`` that is *also* the
name of the surface method that lands it (``op.kind`` → ``surface.<kind>(...)``),
so the driver dispatches with a single ``getattr`` and no lookup table drifts.
``surface_args()`` returns the positional args for that method, so a surface is
freed from re-reading the raw event where the reducer already extracted intent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, Optional, Protocol, Tuple, TypeVar

from mote.contracts.activity import ActivityKind, ActivityNodeState, ActivityOutcome, ActivityTopology
from mote.product.presentation.events import (
    ApprovalRequested,
    ArtifactBlock,
    ErrorRaised,
    FileDiffBlock,
    FoldMode,
    MediaBlock,
    Notice,
    QuestionAsked,
    RetryStatus,
    RuntimeDurabilityStatus,
    SessionListShown,
    SystemReminder,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
    UsageUpdated,
)

EventT = TypeVar("EventT")


class Truncatable(Protocol):
    content_truncated: bool
    hidden_lines: int
    full_ref: Optional[str]


@dataclass(frozen=True)
class Truncation:
    """A small value object describing *why/how* a block or result was folded.

    Carried on :class:`CloseBlock` / :class:`ToolCompleted` so a surface can
    render the fold affordance without re-reading the event. It exposes
    ``full_ref`` and ``hidden_lines`` as plain attributes so the shared
    ``fold_note`` builder (which reads them via ``getattr``) accepts it directly.
    ``fold_mode`` lets a host decide *by data* whether a fold note belongs under
    this row (a Textual ``DETAIL``/``GROUP`` row hides its output inside the
    widget, so no dangling note) instead of a per-host special case.
    """

    content_truncated: bool = False
    hidden_lines: int = 0
    full_ref: Optional[str] = None
    fold_mode: FoldMode = FoldMode.NONE

    @classmethod
    def of(cls, ev: Truncatable, fold_mode: FoldMode = FoldMode.NONE) -> "Truncation":
        """Extract the truncation facts the projector already decided off *ev*."""
        return cls(
            content_truncated=ev.content_truncated,
            hidden_lines=ev.hidden_lines,
            full_ref=ev.full_ref,
            fold_mode=fold_mode,
        )


@dataclass(frozen=True)
class TranscriptOp:
    """Base of the op union — ``kind`` names the surface method that lands it."""

    kind: ClassVar[str] = "transcript_op"

    def surface_args(self) -> Tuple[Any, ...]:
        """Positional args passed to ``surface.<kind>(*args)``. Default: none."""
        return ()


# -- block lifecycle -------------------------------------------------------
@dataclass(frozen=True)
class OpenBlock(TranscriptOp):
    """A streaming message region opened (finalizes any prior open block)."""

    kind: ClassVar[str] = "open_block"
    role: str = "assistant"

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.role,)


@dataclass(frozen=True)
class AppendDelta(TranscriptOp):
    """One streamed chunk of the open block (``reasoning`` = a think token)."""

    kind: ClassVar[str] = "append_delta"
    text: str = ""
    reasoning: bool = False

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.text, self.reasoning)


@dataclass(frozen=True)
class CloseBlock(TranscriptOp):
    """An assistant block completed — finalize (streamed) or render fresh."""

    kind: ClassVar[str] = "close_block"
    markdown: str = ""
    streamed: bool = False
    truncation: Truncation = field(default_factory=Truncation)

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.markdown, self.streamed, self.truncation)


@dataclass(frozen=True)
class RollbackBlock(TranscriptOp):
    """Remove the currently open provisional assistant block."""

    kind: ClassVar[str] = "rollback_block"


@dataclass(frozen=True)
class RenderUserMessage(TranscriptOp):
    """The human's own turn — render as a ``❯`` transcript entry."""

    kind: ClassVar[str] = "render_user_message"
    markdown: str = ""
    message_id: Optional[str] = None

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.markdown, self.message_id)


# -- standalone tools (NONE / DETAIL fold modes) ---------------------------
@dataclass(frozen=True)
class ToolStarted(TranscriptOp):
    """A non-grouping tool started; ``fold`` is its (NONE/DETAIL) fold mode."""

    kind: ClassVar[str] = "tool_started"
    ev: ToolCallStarted
    fold: FoldMode = FoldMode.NONE

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.ev, self.fold)


@dataclass(frozen=True)
class ToolCompleted(TranscriptOp):
    """A non-grouped tool finished."""

    kind: ClassVar[str] = "tool_completed"
    ev: ToolCallCompleted
    fold: FoldMode = FoldMode.NONE
    truncation: Truncation = field(default_factory=Truncation)

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.ev, self.fold, self.truncation)


# -- collapsed search/read group (incremental ops) -------------------------
@dataclass(frozen=True)
class OpenGroup(TranscriptOp):
    """A run of consecutive Read/Grep/Glob calls began coalescing."""

    kind: ClassVar[str] = "open_group"


@dataclass(frozen=True)
class AddToGroup(TranscriptOp):
    """A grouped tool call folded into the open group."""

    kind: ClassVar[str] = "add_to_group"
    ev: ToolCallStarted

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.ev,)


@dataclass(frozen=True)
class CompleteInGroup(TranscriptOp):
    """A grouped tool's result folded into its group."""

    kind: ClassVar[str] = "complete_in_group"
    ev: ToolCallCompleted

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.ev,)


@dataclass(frozen=True)
class FlushGroup(TranscriptOp):
    """The open group ended (a non-transparent event broke the run)."""

    kind: ClassVar[str] = "flush_group"


# -- static transcript rows (pass-through of one event) --------------------
@dataclass(frozen=True)
class _RenderEvent(TranscriptOp, Generic[EventT]):
    """Shared base for the pass-through render ops (carry one event)."""

    ev: EventT

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.ev,)


@dataclass(frozen=True)
class RenderMedia(_RenderEvent[MediaBlock]):
    kind: ClassVar[str] = "render_media"


@dataclass(frozen=True)
class RenderArtifact(_RenderEvent[ArtifactBlock]):
    kind: ClassVar[str] = "render_artifact"


@dataclass(frozen=True)
class RenderFileDiff(_RenderEvent[FileDiffBlock]):
    kind: ClassVar[str] = "render_file_diff"


@dataclass(frozen=True)
class RenderTaskProgress(_RenderEvent[TaskProgress]):
    kind: ClassVar[str] = "render_task_progress"


@dataclass(frozen=True)
class RenderNotice(_RenderEvent[Notice]):
    kind: ClassVar[str] = "render_notice"


@dataclass(frozen=True)
class RenderSystemReminder(_RenderEvent[SystemReminder]):
    kind: ClassVar[str] = "render_system_reminder"


@dataclass(frozen=True)
class RenderError(_RenderEvent[ErrorRaised]):
    kind: ClassVar[str] = "render_error"


@dataclass(frozen=True)
class RenderQuestion(_RenderEvent[QuestionAsked]):
    kind: ClassVar[str] = "render_question"


@dataclass(frozen=True)
class RenderApproval(_RenderEvent[ApprovalRequested]):
    kind: ClassVar[str] = "render_approval"


@dataclass(frozen=True)
class RenderSessionList(_RenderEvent[SessionListShown]):
    kind: ClassVar[str] = "render_session_list"


# -- nested activity (a run_graph orchestration; a sub-agent / bg task) -----
# An activity is keyed by its ``scope`` (a ``ScopePath`` — hashable tuple of
# ScopeRef). The reducer opens one on ActivityStarted, routes every later scoped
# event (per-node progress, the activity's own child tool calls) into it, and
# closes it on ActivityCompleted. A surface owns *how* it renders (terminal:
# append-only topology then outcome block; textual: a live widget that lights
# nodes up) — the reducer only decides *what happened*, host-blind.
@dataclass(frozen=True)
class OpenActivity(TranscriptOp):
    """A nested orchestration began — draw its declared topology."""

    kind: ClassVar[str] = "open_activity"
    scope: Tuple[Any, ...] = ()
    activity_kind: ActivityKind = ActivityKind.GRAPH
    label: str = ""
    topology: ActivityTopology | None = None

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.scope, self.activity_kind, self.label, self.topology)


@dataclass(frozen=True)
class UpdateActivityNode(TranscriptOp):
    """A scoped progress ping updated one node/step of an open activity."""

    kind: ClassVar[str] = "update_activity_node"
    scope: Tuple[Any, ...] = ()
    stage: str = ""
    status: str = ""
    detail: str = ""

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.scope, self.stage, self.status, self.detail)


@dataclass(frozen=True)
class AddActivityToolCall(TranscriptOp):
    """A tool call dispatched *inside* an activity started (folds under it,
    not as a top-level orphan row)."""

    kind: ClassVar[str] = "add_activity_tool_call"
    ev: ToolCallStarted
    scope: Tuple[Any, ...] = ()

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.scope, self.ev)


@dataclass(frozen=True)
class CompleteActivityToolCall(TranscriptOp):
    """A tool call dispatched inside an activity finished (updates the folded row)."""

    kind: ClassVar[str] = "complete_activity_tool_call"
    ev: ToolCallCompleted
    scope: Tuple[Any, ...] = ()

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.scope, self.ev)


@dataclass(frozen=True)
class CloseActivity(TranscriptOp):
    """A nested orchestration finished — render its self-sufficient outcome tree."""

    kind: ClassVar[str] = "close_activity"
    scope: Tuple[Any, ...] = ()
    outcome: ActivityOutcome = ActivityOutcome.SUCCESS
    node_states: tuple[ActivityNodeState, ...] = ()
    summary: str = ""

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.scope, self.outcome, self.node_states, self.summary)


# -- transient chrome (never a permanent transcript row) -------------------
@dataclass(frozen=True)
class SetThinking(TranscriptOp):
    """Toggle the ``✻ 思考中`` reasoning affordance."""

    kind: ClassVar[str] = "set_thinking"
    on: bool = False

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.on,)


@dataclass(frozen=True)
class SetRetry(_RenderEvent[RetryStatus]):
    """Show/refresh the transient LLM-retry countdown."""

    kind: ClassVar[str] = "set_retry"


@dataclass(frozen=True)
class ClearRetry(TranscriptOp):
    """Wipe the transient retry countdown (any other event resolved it)."""

    kind: ClassVar[str] = "clear_retry"


@dataclass(frozen=True)
class UpdateUsage(_RenderEvent[UsageUpdated]):
    """Refresh the token/cost/context usage line."""

    kind: ClassVar[str] = "update_usage"


@dataclass(frozen=True)
class UpdateRuntimeDurability(_RenderEvent[RuntimeDurabilityStatus]):
    """Refresh or clear the managed-runtime durability indicator."""

    kind: ClassVar[str] = "update_runtime_durability"


# -- boundaries / destructive ----------------------------------------------
@dataclass(frozen=True)
class ClearForCompaction(TranscriptOp):
    """A compaction boundary: wipe stale rows + re-render the bridge recap.

    Carries the recap ``summary``, the rebuilt-history ``message_count`` and the
    ``last_user_prompt`` the post-compaction reply continues to answer, so a
    full-screen host can rebuild the bridge without re-reading state.
    """

    kind: ClassVar[str] = "clear_for_compaction"
    summary: str = ""
    message_count: int = 0
    last_user_prompt: str = ""

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.summary, self.message_count, self.last_user_prompt)


@dataclass(frozen=True)
class ClearTranscript(TranscriptOp):
    """``/clear`` — drop every rendered row for a fresh screen."""

    kind: ClassVar[str] = "clear_transcript"


__all__ = [
    "Truncation",
    "TranscriptOp",
    "OpenBlock",
    "AppendDelta",
    "CloseBlock",
    "RollbackBlock",
    "RenderUserMessage",
    "ToolStarted",
    "ToolCompleted",
    "OpenGroup",
    "AddToGroup",
    "CompleteInGroup",
    "FlushGroup",
    "RenderMedia",
    "RenderArtifact",
    "RenderFileDiff",
    "RenderTaskProgress",
    "RenderNotice",
    "RenderSystemReminder",
    "RenderError",
    "RenderQuestion",
    "RenderApproval",
    "RenderSessionList",
    "OpenActivity",
    "UpdateActivityNode",
    "AddActivityToolCall",
    "CompleteActivityToolCall",
    "CloseActivity",
    "SetThinking",
    "SetRetry",
    "ClearRetry",
    "UpdateUsage",
    "UpdateRuntimeDurability",
    "ClearForCompaction",
    "ClearTranscript",
]
