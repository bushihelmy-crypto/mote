#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TranscriptOp`` — the neutral op vocabulary the reducer emits.

A :class:`~mote.cli.consumers.transcript.reducer.TranscriptReducer` folds the
``ViewEvent`` stream into a stream of these ops: *what happened*, fully
semantic, with **zero host awareness**. Each host then implements a thin
:class:`~mote.cli.consumers.transcript.surface.RenderSurface` — one method per
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
from typing import Any, ClassVar, Optional, Tuple

from mote.cli.consumers.render.builders import FoldMode


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
    def of(cls, ev: Any, fold_mode: FoldMode = FoldMode.NONE) -> "Truncation":
        """Extract the truncation facts the projector already decided off *ev*."""
        return cls(
            content_truncated=bool(getattr(ev, "content_truncated", False)),
            hidden_lines=int(getattr(ev, "hidden_lines", 0) or 0),
            full_ref=getattr(ev, "full_ref", None),
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
class RenderUserMessage(TranscriptOp):
    """The human's own turn — render as a ``❯`` transcript entry."""

    kind: ClassVar[str] = "render_user_message"
    markdown: str = ""

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.markdown,)


# -- standalone tools (NONE / DETAIL fold modes) ---------------------------
@dataclass(frozen=True)
class ToolStarted(TranscriptOp):
    """A non-grouping tool started; ``fold`` is its (NONE/DETAIL) fold mode."""

    kind: ClassVar[str] = "tool_started"
    ev: Any = None
    fold: FoldMode = FoldMode.NONE

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.ev, self.fold)


@dataclass(frozen=True)
class ToolCompleted(TranscriptOp):
    """A non-grouped tool finished."""

    kind: ClassVar[str] = "tool_completed"
    ev: Any = None
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
    ev: Any = None

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.ev,)


@dataclass(frozen=True)
class CompleteInGroup(TranscriptOp):
    """A grouped tool's result folded into its group."""

    kind: ClassVar[str] = "complete_in_group"
    ev: Any = None

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.ev,)


@dataclass(frozen=True)
class FlushGroup(TranscriptOp):
    """The open group ended (a non-transparent event broke the run)."""

    kind: ClassVar[str] = "flush_group"


# -- static transcript rows (pass-through of one event) --------------------
@dataclass(frozen=True)
class _RenderEvent(TranscriptOp):
    """Shared base for the pass-through render ops (carry one event)."""

    ev: Any = None

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.ev,)


@dataclass(frozen=True)
class RenderMedia(_RenderEvent):
    kind: ClassVar[str] = "render_media"


@dataclass(frozen=True)
class RenderFileDiff(_RenderEvent):
    kind: ClassVar[str] = "render_file_diff"


@dataclass(frozen=True)
class RenderTaskProgress(_RenderEvent):
    kind: ClassVar[str] = "render_task_progress"


@dataclass(frozen=True)
class RenderNotice(_RenderEvent):
    kind: ClassVar[str] = "render_notice"


@dataclass(frozen=True)
class RenderSystemReminder(_RenderEvent):
    kind: ClassVar[str] = "render_system_reminder"


@dataclass(frozen=True)
class RenderError(_RenderEvent):
    kind: ClassVar[str] = "render_error"


@dataclass(frozen=True)
class RenderQuestion(_RenderEvent):
    kind: ClassVar[str] = "render_question"


@dataclass(frozen=True)
class RenderApproval(_RenderEvent):
    kind: ClassVar[str] = "render_approval"


@dataclass(frozen=True)
class RenderSessionList(_RenderEvent):
    kind: ClassVar[str] = "render_session_list"


# -- transient chrome (never a permanent transcript row) -------------------
@dataclass(frozen=True)
class SetThinking(TranscriptOp):
    """Toggle the ``✻ 思考中`` reasoning affordance."""

    kind: ClassVar[str] = "set_thinking"
    on: bool = False

    def surface_args(self) -> Tuple[Any, ...]:
        return (self.on,)


@dataclass(frozen=True)
class SetRetry(_RenderEvent):
    """Show/refresh the transient LLM-retry countdown."""

    kind: ClassVar[str] = "set_retry"


@dataclass(frozen=True)
class ClearRetry(TranscriptOp):
    """Wipe the transient retry countdown (any other event resolved it)."""

    kind: ClassVar[str] = "clear_retry"


@dataclass(frozen=True)
class UpdateUsage(_RenderEvent):
    """Refresh the token/cost/context usage line."""

    kind: ClassVar[str] = "update_usage"


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
    "RenderUserMessage",
    "ToolStarted",
    "ToolCompleted",
    "OpenGroup",
    "AddToGroup",
    "CompleteInGroup",
    "FlushGroup",
    "RenderMedia",
    "RenderFileDiff",
    "RenderTaskProgress",
    "RenderNotice",
    "RenderSystemReminder",
    "RenderError",
    "RenderQuestion",
    "RenderApproval",
    "RenderSessionList",
    "SetThinking",
    "SetRetry",
    "ClearRetry",
    "UpdateUsage",
    "ClearForCompaction",
    "ClearTranscript",
]
