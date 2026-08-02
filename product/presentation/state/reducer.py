#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TranscriptReducer`` — the single, host-agnostic event orchestration machine.

``feed(view_event) -> list[TranscriptOp]`` is the *only* place the timing
semantics live: when a run of Read/Grep/Glob coalesces into one group, when a
transient (retry countdown / thinking label) is cleared, when an assistant
block closes, when a compaction wipes the screen. Both rich hosts (the scrolling
terminal and the full-screen Textual app) drove these decisions in their own
``on_view_event`` choke before; this reducer merges the two into one so they can
never drift, and a future host (web/IM) inherits the behaviour for free by
implementing a :class:`~mote.product.presentation.state.surface.RenderSurface`.

The reducer holds only the bookkeeping both hosts already kept (merged): the
open block / open group / grouped-id set / thinking / retry-active flags and the
last user prompt. It applies three cross-cutting rules up front (as prefix ops)
before folding the event by kind — the three guards that used to sit at the top
of each host's choke:

1. **retry clear** — any event other than a ``retry_status`` resolves an
   in-flight retry, so its transient countdown is wiped first.
2. **group break** — any *non-transparent* event ends an open search/read run.
3. **thinking end** — any *non-transparent* event leaves the ``✻ 思考中`` state.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional, Set, Tuple

from mote.contracts.async_work.codec import decode_async_work_observation
from mote.contracts.async_work.observation import DurableWorkflowRunObservation, LocalBackgroundTaskObservation
from mote.product.presentation.events import (
    ACTIVITY_COMPLETED,
    ACTIVITY_STARTED,
    ASYNC_WORK_OBSERVED,
    ATTEMPT_STREAM_COMMITTED,
    ATTEMPT_STREAM_DISCARDED,
    ATTEMPT_STREAM_INTERRUPTED,
    REASONING_DELTA,
    RETRY_STATUS,
    RUNTIME_DURABILITY_STATUS,
    SESSION_LIST_SHOWN,
    TASK_PROGRESS,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_STARTED,
    TRANSCRIPT_CLEARED,
    USAGE_UPDATED,
    ActivityCompleted,
    ActivityStarted,
    ApprovalRequested,
    ArtifactBlock,
    AsyncWorkObserved,
    ConversationCompacted,
    ErrorRaised,
    FileDiffBlock,
    FoldMode,
    MediaBlock,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    Notice,
    QuestionAsked,
    ReasoningDelta,
    RetryStatus,
    RuntimeDurabilityStatus,
    SessionListShown,
    SystemReminder,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
    UsageUpdated,
    ViewEvent,
    fold_mode,
)
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
    Truncation,
    UpdateActivityNode,
    UpdateRuntimeDurability,
    UpdateUsage,
)


def _render_async_work_detail(observation) -> str:
    actions = ",".join(action.value for action in observation.available_actions)
    if isinstance(observation, LocalBackgroundTaskObservation):
        reference = observation.reference.reference
        return (
            "local background task | "
            f"agent_id={reference.owner.agent_id} "
            f"task_id={reference.task_id} attempt_id={reference.attempt_id} | "
            f"actions={actions or 'none'}"
        )
    if isinstance(observation, DurableWorkflowRunObservation):
        reference = observation.reference.reference
        return (
            "durable workflow | "
            f"run_id={reference.run_id} definition_id={reference.definition_id} "
            f"revision={observation.revision} | actions={actions or 'none'}"
        )
    raise TypeError("Unknown async-work observation variant")


# Events that DON'T break an open search/read group: the grouped tools' own
# start/complete, and status-only events that mutate no transcript row. (A
# non-grouping tool start IS a ``tool_call_started`` — transparent here — so the
# group break for it is emitted explicitly in the per-kind fold, mirroring how
# each host flushed inside its tool handler.)
_GROUP_TRANSPARENT = frozenset(
    {
        TOOL_CALL_STARTED,
        TOOL_CALL_COMPLETED,
        RETRY_STATUS,
        RUNTIME_DURABILITY_STATUS,
        USAGE_UPDATED,
        SESSION_LIST_SHOWN,
        # An in-flight activity's own lifecycle/progress must not flush an open
        # Read/Grep group — the activity nests its child rows, it does not break
        # a sibling run (same rationale as ``USAGE_UPDATED``).
        ACTIVITY_STARTED,
        ACTIVITY_COMPLETED,
        TASK_PROGRESS,
        ASYNC_WORK_OBSERVED,
    }
)

# Events that DON'T end the ``✻ 思考中`` reasoning state: the reasoning stream
# itself, and status-only events that mutate no transcript row.
_THINKING_TRANSPARENT = frozenset(
    {
        REASONING_DELTA,
        USAGE_UPDATED,
        RETRY_STATUS,
        RUNTIME_DURABILITY_STATUS,
        # A scoped activity ping arriving mid-thought is background progress, not
        # a turn boundary — it must not end the reasoning affordance.
        ACTIVITY_STARTED,
        ACTIVITY_COMPLETED,
        TASK_PROGRESS,
        ASYNC_WORK_OBSERVED,
    }
)


class TranscriptReducer:
    """Fold the ``ViewEvent`` stream into a neutral :class:`TranscriptOp` stream."""

    def __init__(self) -> None:
        self._block_open = False
        self._group_open = False
        # Invocation ids that were folded into *a* group and are still awaiting
        # their completion. Retained across a flush so a grouped tool that
        # completes *after* an interrupting event still folds into its group
        # (a tool is allowed to finish after its run was broken).
        self._grouped_ids: Set[str] = set()
        self._thinking = False
        self._retry_active = False
        self._last_user_prompt = ""
        # Open nested orchestrations keyed by their ``scope`` (a hashable
        # ScopePath tuple). A scoped progress ping / child tool call whose head
        # names an open activity folds *into* it instead of orphaning at top
        # level. Value is the ``activity_kind`` (kept for potential per-kind
        # routing; the surface owns rendering).
        self._activities: dict[Tuple[Any, ...], str] = {}

    def feed(self, ev: ViewEvent) -> List[TranscriptOp]:
        """Fold one event into zero-or-more ops (prefix guards, then the fold)."""
        kind = ev.kind
        ops: List[TranscriptOp] = []

        # 1. retry clear — any non-retry event resolves an in-flight countdown.
        if kind != RETRY_STATUS and self._retry_active:
            self._retry_active = False
            ops.append(ClearRetry())
        # 2. group break — any non-transparent event ends an open run.
        if kind not in _GROUP_TRANSPARENT and self._group_open:
            self._group_open = False
            ops.append(FlushGroup())
        # 3. thinking end — any non-transparent event leaves the thinking state.
        if kind not in _THINKING_TRANSPARENT and self._thinking:
            self._thinking = False
            ops.append(SetThinking(on=False))

        ops.extend(self._fold(ev, kind))
        return ops

    # ------------------------------------------------------------------
    # Per-kind fold
    # ------------------------------------------------------------------
    def _fold(self, ev: ViewEvent, kind: str) -> List[TranscriptOp]:
        if isinstance(ev, MessageBlockStarted):
            self._block_open = False
            return [OpenBlock(role=ev.role)]

        if isinstance(ev, MessageBlockDelta):
            self._block_open = True
            return [AppendDelta(text=ev.text, reasoning=False)]

        if kind == ATTEMPT_STREAM_COMMITTED:
            return []

        if kind in {ATTEMPT_STREAM_DISCARDED, ATTEMPT_STREAM_INTERRUPTED}:
            self._block_open = False
            return [RollbackBlock()]

        if isinstance(ev, ReasoningDelta):
            self._block_open = True
            ops: List[TranscriptOp] = []
            if not self._thinking:
                self._thinking = True
                ops.append(SetThinking(on=True))
            ops.append(AppendDelta(text=ev.text, reasoning=True))
            return ops

        if isinstance(ev, MessageBlockCompleted):
            self._block_open = False
            markdown = ev.markdown
            if ev.role == "user":
                if markdown.strip():
                    self._last_user_prompt = markdown
                return [RenderUserMessage(markdown=markdown, message_id=ev.message_id)]
            return [
                CloseBlock(
                    markdown=markdown,
                    streamed=ev.streamed,
                    truncation=Truncation.of(ev),
                )
            ]

        if isinstance(ev, ToolCallStarted):
            return self._fold_tool_started(ev)

        if isinstance(ev, ToolCallCompleted):
            return self._fold_tool_completed(ev)

        if isinstance(ev, ActivityStarted):
            scope = self._scope_of(ev)
            self._activities[scope] = ev.activity_kind
            return [
                OpenActivity(
                    scope=scope,
                    activity_kind=ev.activity_kind,
                    label=ev.label,
                    topology=ev.topology,
                )
            ]
        if isinstance(ev, ActivityCompleted):
            scope = self._scope_of(ev)
            self._activities.pop(scope, None)
            return [
                CloseActivity(
                    scope=scope,
                    outcome=ev.outcome,
                    node_states=tuple(ev.node_states),
                    summary=ev.summary,
                )
            ]

        if isinstance(ev, MediaBlock):
            return [RenderMedia(ev=ev)]
        if isinstance(ev, ArtifactBlock):
            return [RenderArtifact(ev=ev)]
        if isinstance(ev, FileDiffBlock):
            return [RenderFileDiff(ev=ev)]
        if isinstance(ev, TaskProgress):
            # A scoped ping whose head names an open activity updates its
            # subtree; an unscoped ping (background task) stays a top-level row.
            # The op is keyed by the OWNING activity's scope (the matched prefix,
            # e.g. ``(graph,)``), not the ping's own longer ``(graph, node)`` —
            # that is the key the surface stored the widget under.
            scope = self._scope_of(ev)
            owning = self._owning_activity(scope) if scope else None
            if owning is not None:
                return [
                    UpdateActivityNode(
                        scope=owning,
                        stage=ev.stage,
                        status=ev.status,
                        detail=ev.detail,
                    )
                ]
            return [RenderTaskProgress(ev=ev)]
        if isinstance(ev, AsyncWorkObserved):
            observation = decode_async_work_observation(json.loads(ev.observation_json))
            return [
                RenderTaskProgress(
                    ev=TaskProgress(
                        stage="async-work",
                        status=observation.phase.value,
                        detail=_render_async_work_detail(observation),
                    )
                )
            ]
        if isinstance(ev, Notice):
            return [RenderNotice(ev=ev)]
        if isinstance(ev, SystemReminder):
            return [RenderSystemReminder(ev=ev)]
        if isinstance(ev, ErrorRaised):
            return [RenderError(ev=ev)]
        if isinstance(ev, QuestionAsked):
            return [RenderQuestion(ev=ev)]
        if isinstance(ev, ApprovalRequested):
            return [RenderApproval(ev=ev)]
        if isinstance(ev, SessionListShown):
            return [RenderSessionList(ev=ev)]

        if isinstance(ev, UsageUpdated):
            return [UpdateUsage(ev=ev)]
        if isinstance(ev, RuntimeDurabilityStatus):
            return [UpdateRuntimeDurability(ev=ev)]
        if isinstance(ev, RetryStatus):
            self._retry_active = True
            return [SetRetry(ev=ev)]

        if isinstance(ev, ConversationCompacted):
            self._reset_transcript_state()
            return [
                ClearForCompaction(
                    summary=ev.summary,
                    message_count=ev.message_count,
                    last_user_prompt=self._last_user_prompt,
                )
            ]
        if kind == TRANSCRIPT_CLEARED:
            self._reset_transcript_state()
            return [ClearTranscript()]

        return []

    def _fold_tool_started(self, ev: ToolCallStarted) -> List[TranscriptOp]:
        # Orphan fix: a tool dispatched *inside* an activity (its scope's head is
        # an open activity) folds under that activity instead of orphaning as a
        # top-level row. Every Tool call has an execution-owner identity. The op
        # is keyed by the OWNING activity's scope (the matched prefix), not the
        # child's own longer scope — that is the surface's widget key.
        scope = self._scope_of(ev)
        owning = self._owning_activity(scope) if scope else None
        if owning is not None:
            return [AddActivityToolCall(scope=owning, ev=ev)]
        fold = fold_mode(ev.tool_name)
        tid = str(ev.identity.invocation_id)
        if fold is FoldMode.GROUP:
            ops: List[TranscriptOp] = []
            if not self._group_open:
                self._group_open = True
                ops.append(OpenGroup())
            self._grouped_ids.add(tid)
            ops.append(AddToGroup(ev=ev))
            return ops
        # A non-grouping tool (NONE/DETAIL) breaks an open run, then stands alone.
        # ``tool_call_started`` is group-transparent, so the flush is emitted here
        # (not by the front-of-feed guard) — mirroring the host handlers.
        ops = []
        if self._group_open:
            self._group_open = False
            ops.append(FlushGroup())
        ops.append(ToolStarted(ev=ev, fold=fold))
        return ops

    def _fold_tool_completed(self, ev: ToolCallCompleted) -> List[TranscriptOp]:
        scope = self._scope_of(ev)
        owning = self._owning_activity(scope) if scope else None
        if owning is not None:
            return [CompleteActivityToolCall(scope=owning, ev=ev)]
        tid = str(ev.identity.invocation_id)
        if tid in self._grouped_ids:
            self._grouped_ids.discard(tid)
            return [CompleteInGroup(ev=ev)]
        fold = fold_mode(ev.tool_name)
        return [ToolCompleted(ev=ev, fold=fold, truncation=Truncation.of(ev, fold))]

    @staticmethod
    def _scope_of(ev: ViewEvent) -> Tuple[Any, ...]:
        """Read the (possibly empty) ``scope`` path off an event as a tuple."""
        return tuple(ev.scope)

    def _owning_activity(self, scope: Tuple[Any, ...]) -> Optional[Tuple[Any, ...]]:
        """Return the *scope key* of the open activity that owns ``scope``.

        A scoped event belongs to an activity whose scope is a *prefix* of it
        (the activity's own scope, or an ancestor for a child tool call whose
        path is ``(graph, node)``). Longest matching prefix wins so a nested
        activity routes to the innermost open one. ``None`` when nothing open
        owns it (e.g. a background task's unscoped ping).

        The RETURN is the matched activity's own scope — the exact key the
        surface stored its widget under (via ``OpenActivity``). A child event's
        scope is longer (``(graph, node)``) than the activity's (``(graph,)``),
        so routing an update to the child's own scope would miss the widget;
        callers key the op by this returned prefix instead.
        """
        best: Optional[Tuple[Any, ...]] = None
        best_len = -1
        for open_scope in self._activities:
            n = len(open_scope)
            if n > best_len and scope[:n] == open_scope:
                best = open_scope
                best_len = n
        return best

    def _reset_transcript_state(self) -> None:
        """Drop the open-block / open-group bookkeeping on a screen wipe."""
        self._block_open = False
        self._group_open = False
        self._grouped_ids.clear()
        self._activities.clear()


__all__ = ["TranscriptReducer"]
