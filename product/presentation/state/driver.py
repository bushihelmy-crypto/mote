#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``SurfaceDriver`` — glue a :class:`TranscriptReducer` to a :class:`RenderSurface`.

Itself a :class:`~mote.product.presentation.consumer.BaseConsumer`, so a host wires
``SurfaceDriver(surface=…)`` into ``build_app(consumer_objs=[…])`` exactly like
any other consumer — the projector / :class:`CapabilityAdapter` path is
untouched. It declares ``TERMINAL_CAPS`` (streaming on) so it receives raw
per-token deltas.

It defines **no** ``on_<kind>`` handlers, so every event — the async block/tool
path (``handle``) and the sync delta/progress path (``handle_sync``) — routes to
``on_unhandled``, which folds the event through the reducer and lands each op on
the surface. Reducer folds and surface methods are all synchronous (a terminal
prints inline; a Textual surface mutates widgets on the UI pump it already runs
on), so both dispatch paths work without ceremony.
"""

from __future__ import annotations

from typing import Optional

from mote.product.presentation.consumer import BaseConsumer
from mote.product.presentation.events import TERMINAL_CAPS, Capabilities, ViewEvent
from mote.product.presentation.state.ops import (
    AddActivityToolCall,
    AddToGroup,
    AppendDelta,
    ClearForCompaction,
    ClearRetry,
    ClearTranscript,
    CloseActivity,
    CloseBlock,
    CompleteActivityToolCall,
    CompleteInGroup,
    FlushGroup,
    OpenActivity,
    OpenBlock,
    OpenGroup,
    RenderApproval,
    RenderArtifact,
    RenderError,
    RenderFileDiff,
    RenderMedia,
    RenderNotice,
    RenderQuestion,
    RenderSessionList,
    RenderSystemReminder,
    RenderTaskProgress,
    RenderUserMessage,
    RollbackBlock,
    SetRetry,
    SetThinking,
    ToolCompleted,
    ToolStarted,
    TranscriptOp,
    UpdateActivityNode,
    UpdateRuntimeDurability,
    UpdateUsage,
)
from mote.product.presentation.state.reducer import TranscriptReducer
from mote.product.presentation.state.surface import RenderSurface


def apply_ops(reducer: TranscriptReducer, surface: RenderSurface, ev: ViewEvent) -> None:
    """Fold one ``ViewEvent`` through the reducer and land each op on the surface.

    The single host-blind dispatch step: ``op.kind`` is the surface method name,
    so one ``getattr`` routes with no lookup table to drift. Both rich hosts call
    this — the terminal via :class:`SurfaceDriver` (inline, on the consumer thread)
    and the Textual app inline on its UI pump — so "how an op lands on a surface"
    is written exactly once.
    """
    ops = reducer.feed(ev)
    for op in ops:
        apply_op(surface, op)


def apply_op(surface: RenderSurface, op: TranscriptOp) -> None:
    """Land one closed transcript operation without reflective dispatch."""
    if isinstance(op, OpenBlock):
        surface.open_block(op.role)
    elif isinstance(op, AppendDelta):
        surface.append_delta(op.text, op.reasoning)
    elif isinstance(op, CloseBlock):
        surface.close_block(op.markdown, op.streamed, op.truncation)
    elif isinstance(op, RollbackBlock):
        surface.rollback_block()
    elif isinstance(op, RenderUserMessage):
        surface.render_user_message(op.markdown, op.message_id)
    elif isinstance(op, ToolStarted):
        surface.tool_started(op.ev, op.fold)
    elif isinstance(op, ToolCompleted):
        surface.tool_completed(op.ev, op.fold, op.truncation)
    elif isinstance(op, OpenGroup):
        surface.open_group()
    elif isinstance(op, AddToGroup):
        surface.add_to_group(op.ev)
    elif isinstance(op, CompleteInGroup):
        surface.complete_in_group(op.ev)
    elif isinstance(op, FlushGroup):
        surface.flush_group()
    elif isinstance(op, OpenActivity):
        surface.open_activity(op.scope, op.activity_kind, op.label, op.topology)
    elif isinstance(op, UpdateActivityNode):
        surface.update_activity_node(op.scope, op.stage, op.status, op.detail)
    elif isinstance(op, AddActivityToolCall):
        surface.add_activity_tool_call(op.scope, op.ev)
    elif isinstance(op, CompleteActivityToolCall):
        surface.complete_activity_tool_call(op.scope, op.ev)
    elif isinstance(op, CloseActivity):
        surface.close_activity(op.scope, op.outcome, op.node_states, op.summary)
    elif isinstance(op, RenderMedia):
        surface.render_media(op.ev)
    elif isinstance(op, RenderArtifact):
        surface.render_artifact(op.ev)
    elif isinstance(op, RenderFileDiff):
        surface.render_file_diff(op.ev)
    elif isinstance(op, RenderTaskProgress):
        surface.render_task_progress(op.ev)
    elif isinstance(op, RenderNotice):
        surface.render_notice(op.ev)
    elif isinstance(op, RenderSystemReminder):
        surface.render_system_reminder(op.ev)
    elif isinstance(op, RenderError):
        surface.render_error(op.ev)
    elif isinstance(op, RenderQuestion):
        surface.render_question(op.ev)
    elif isinstance(op, RenderApproval):
        surface.render_approval(op.ev)
    elif isinstance(op, RenderSessionList):
        surface.render_session_list(op.ev)
    elif isinstance(op, SetThinking):
        surface.set_thinking(op.on)
    elif isinstance(op, SetRetry):
        surface.set_retry(op.ev)
    elif isinstance(op, ClearRetry):
        surface.clear_retry()
    elif isinstance(op, UpdateUsage):
        surface.update_usage(op.ev)
    elif isinstance(op, UpdateRuntimeDurability):
        surface.update_runtime_durability(op.ev)
    elif isinstance(op, ClearForCompaction):
        surface.clear_for_compaction(op.summary, op.message_count, op.last_user_prompt)
    elif isinstance(op, ClearTranscript):
        surface.clear_transcript()


class SurfaceDriver(BaseConsumer):
    """Reducer + surface, packaged as a streaming ``BaseConsumer``."""

    capabilities: Capabilities = TERMINAL_CAPS

    def __init__(self, surface: RenderSurface, reducer: Optional[TranscriptReducer] = None) -> None:
        self._surface = surface
        self._reducer = reducer if reducer is not None else TranscriptReducer()

    def on_unhandled(self, ev: ViewEvent) -> None:
        # No ``on_<kind>`` methods exist, so every event (async or sync path)
        # lands here and is folded through the single reducer.
        apply_ops(self._reducer, self._surface, ev)

    async def aclose(self) -> None:
        self._surface.close()


__all__ = ["SurfaceDriver", "apply_op", "apply_ops"]
