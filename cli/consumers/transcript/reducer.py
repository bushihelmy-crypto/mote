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
implementing a :class:`~mote.cli.consumers.transcript.surface.RenderSurface`.

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

from typing import Any, List, Optional, Set, Tuple

from mote.cli.consumers.render.builders import FoldMode, fold_mode
from mote.cli.consumers.transcript.ops import (
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
    RenderError,
    RenderFileDiff,
    RenderMedia,
    RenderNotice,
    RenderQuestion,
    RenderSessionList,
    RenderSystemReminder,
    RenderTaskProgress,
    RenderUserMessage,
    SetRetry,
    SetThinking,
    ToolCompleted,
    ToolStarted,
    TranscriptOp,
    Truncation,
    UpdateActivityNode,
    UpdateUsage,
)
from mote.cli.contracts.view import (
    ACTIVITY_COMPLETED,
    ACTIVITY_STARTED,
    APPROVAL_REQUESTED,
    CONVERSATION_COMPACTED,
    ERROR_RAISED,
    FILE_DIFF_BLOCK,
    MEDIA_BLOCK,
    MESSAGE_BLOCK_COMPLETED,
    MESSAGE_BLOCK_DELTA,
    MESSAGE_BLOCK_STARTED,
    NOTICE,
    QUESTION_ASKED,
    REASONING_DELTA,
    RETRY_STATUS,
    SESSION_LIST_SHOWN,
    SYSTEM_REMINDER,
    TASK_PROGRESS,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_STARTED,
    TRANSCRIPT_CLEARED,
    USAGE_UPDATED,
)

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
        USAGE_UPDATED,
        SESSION_LIST_SHOWN,
        # An in-flight activity's own lifecycle/progress must not flush an open
        # Read/Grep group — the activity nests its child rows, it does not break
        # a sibling run (same rationale as ``USAGE_UPDATED``).
        ACTIVITY_STARTED,
        ACTIVITY_COMPLETED,
        TASK_PROGRESS,
    }
)

# Events that DON'T end the ``✻ 思考中`` reasoning state: the reasoning stream
# itself, and status-only events that mutate no transcript row.
_THINKING_TRANSPARENT = frozenset(
    {
        REASONING_DELTA,
        USAGE_UPDATED,
        RETRY_STATUS,
        # A scoped activity ping arriving mid-thought is background progress, not
        # a turn boundary — it must not end the reasoning affordance.
        ACTIVITY_STARTED,
        ACTIVITY_COMPLETED,
        TASK_PROGRESS,
    }
)


class TranscriptReducer:
    """Fold the ``ViewEvent`` stream into a neutral :class:`TranscriptOp` stream."""

    def __init__(self) -> None:
        self._block_open = False
        self._group_open = False
        # tool_use_ids that were folded into *a* group and are still awaiting
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

    def feed(self, ev: Any) -> List[TranscriptOp]:
        """Fold one event into zero-or-more ops (prefix guards, then the fold)."""
        kind = getattr(ev, "kind", None)
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
    def _fold(self, ev: Any, kind: Any) -> List[TranscriptOp]:
        if kind == MESSAGE_BLOCK_STARTED:
            self._block_open = False
            return [OpenBlock(role=getattr(ev, "role", "assistant"))]

        if kind == MESSAGE_BLOCK_DELTA:
            self._block_open = True
            return [AppendDelta(text=getattr(ev, "text", ""), reasoning=False)]

        if kind == REASONING_DELTA:
            self._block_open = True
            ops: List[TranscriptOp] = []
            if not self._thinking:
                self._thinking = True
                ops.append(SetThinking(on=True))
            ops.append(AppendDelta(text=getattr(ev, "text", ""), reasoning=True))
            return ops

        if kind == MESSAGE_BLOCK_COMPLETED:
            self._block_open = False
            markdown = getattr(ev, "markdown", "") or ""
            if getattr(ev, "role", "assistant") == "user":
                if markdown.strip():
                    self._last_user_prompt = markdown
                return [RenderUserMessage(markdown=markdown, message_id=getattr(ev, "message_id", None))]
            return [
                CloseBlock(
                    markdown=markdown,
                    streamed=bool(getattr(ev, "streamed", False)),
                    truncation=Truncation.of(ev),
                )
            ]

        if kind == TOOL_CALL_STARTED:
            return self._fold_tool_started(ev)

        if kind == TOOL_CALL_COMPLETED:
            return self._fold_tool_completed(ev)

        if kind == ACTIVITY_STARTED:
            scope = self._scope_of(ev)
            self._activities[scope] = getattr(ev, "activity_kind", "") or ""
            return [
                OpenActivity(
                    scope=scope,
                    activity_kind=getattr(ev, "activity_kind", "") or "",
                    label=getattr(ev, "label", "") or "",
                    topology=getattr(ev, "topology", None),
                )
            ]
        if kind == ACTIVITY_COMPLETED:
            scope = self._scope_of(ev)
            self._activities.pop(scope, None)
            return [
                CloseActivity(
                    scope=scope,
                    outcome=getattr(ev, "outcome", "success") or "success",
                    node_states=tuple(getattr(ev, "node_states", ()) or ()),
                    summary=getattr(ev, "summary", "") or "",
                )
            ]

        if kind == MEDIA_BLOCK:
            return [RenderMedia(ev=ev)]
        if kind == FILE_DIFF_BLOCK:
            return [RenderFileDiff(ev=ev)]
        if kind == TASK_PROGRESS:
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
                        stage=getattr(ev, "stage", "") or "",
                        status=getattr(ev, "status", "") or "",
                        detail=getattr(ev, "detail", "") or "",
                    )
                ]
            return [RenderTaskProgress(ev=ev)]
        if kind == NOTICE:
            return [RenderNotice(ev=ev)]
        if kind == SYSTEM_REMINDER:
            return [RenderSystemReminder(ev=ev)]
        if kind == ERROR_RAISED:
            return [RenderError(ev=ev)]
        if kind == QUESTION_ASKED:
            return [RenderQuestion(ev=ev)]
        if kind == APPROVAL_REQUESTED:
            return [RenderApproval(ev=ev)]
        if kind == SESSION_LIST_SHOWN:
            return [RenderSessionList(ev=ev)]

        if kind == USAGE_UPDATED:
            return [UpdateUsage(ev=ev)]
        if kind == RETRY_STATUS:
            self._retry_active = True
            return [SetRetry(ev=ev)]

        if kind == CONVERSATION_COMPACTED:
            self._reset_transcript_state()
            return [
                ClearForCompaction(
                    summary=getattr(ev, "summary", "") or "",
                    message_count=int(getattr(ev, "message_count", 0) or 0),
                    last_user_prompt=self._last_user_prompt,
                )
            ]
        if kind == TRANSCRIPT_CLEARED:
            self._reset_transcript_state()
            return [ClearTranscript()]

        return []

    def _fold_tool_started(self, ev: Any) -> List[TranscriptOp]:
        # Orphan fix: a tool dispatched *inside* an activity (its scope's head is
        # an open activity) folds under that activity instead of orphaning as a
        # top-level row (graph-internal calls carry ``tool_use_id=None``). The op
        # is keyed by the OWNING activity's scope (the matched prefix), not the
        # child's own longer scope — that is the surface's widget key.
        scope = self._scope_of(ev)
        owning = self._owning_activity(scope) if scope else None
        if owning is not None:
            return [AddActivityToolCall(scope=owning, ev=ev)]
        fold = fold_mode(getattr(ev, "tool_name", "") or "")
        tid = getattr(ev, "tool_use_id", None)
        if fold is FoldMode.GROUP:
            ops: List[TranscriptOp] = []
            if not self._group_open:
                self._group_open = True
                ops.append(OpenGroup())
            if tid:
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

    def _fold_tool_completed(self, ev: Any) -> List[TranscriptOp]:
        scope = self._scope_of(ev)
        owning = self._owning_activity(scope) if scope else None
        if owning is not None:
            return [CompleteActivityToolCall(scope=owning, ev=ev)]
        tid = getattr(ev, "tool_use_id", None)
        if tid and tid in self._grouped_ids:
            self._grouped_ids.discard(tid)
            return [CompleteInGroup(ev=ev)]
        fold = fold_mode(getattr(ev, "tool_name", "") or "")
        return [ToolCompleted(ev=ev, fold=fold, truncation=Truncation.of(ev, fold))]

    @staticmethod
    def _scope_of(ev: Any) -> Tuple[Any, ...]:
        """Read the (possibly empty) ``scope`` path off an event as a tuple."""
        return tuple(getattr(ev, "scope", ()) or ())

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
