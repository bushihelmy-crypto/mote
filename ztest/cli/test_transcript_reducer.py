#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TranscriptReducer`` — the single host-agnostic orchestration machine.

These assert the *op stream* the reducer folds a ``ViewEvent`` sequence into —
the one place the timing semantics (group coalescing/breaking, transient
clearing, thinking on/off, compaction reset) live now that both rich hosts share
them. Because the ops are host-blind, an assertion here proves BOTH hosts make
the same decision (the terminal + Textual surfaces only *land* these ops).
"""

from __future__ import annotations

from mote.cli.consumers.render.builders import FoldMode
from mote.cli.consumers.transcript import (
    AddToGroup,
    AppendDelta,
    ClearForCompaction,
    ClearRetry,
    ClearTranscript,
    CloseBlock,
    CompleteInGroup,
    FlushGroup,
    OpenBlock,
    OpenGroup,
    RenderError,
    RenderNotice,
    RenderTaskProgress,
    RenderUserMessage,
    SetRetry,
    SetThinking,
    ToolCompleted,
    ToolStarted,
    TranscriptReducer,
    UpdateUsage,
)
from mote.cli.consumers.transcript.ops import (
    AddActivityToolCall,
    CloseActivity,
    CompleteActivityToolCall,
    OpenActivity,
    UpdateActivityNode,
)
from mote.cli.contracts.view import (
    ActivityCompleted,
    ActivityStarted,
    ConversationCompacted,
    ErrorRaised,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    Notice,
    ReasoningDelta,
    RetryStatus,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
    TranscriptCleared,
    UsageUpdated,
)


def _kinds(ops):
    return [type(op).kind for op in ops]


def _started(name, tid, headline=""):
    return ToolCallStarted(tool_name=name, headline=headline, tool_use_id=tid)


def _completed(name, tid, summary="ok"):
    return ToolCallCompleted(tool_name=name, tool_use_id=tid, summary=summary)


# ---------------------------------------------------------------- block ---
def test_block_started_delta_completed_streamed():
    r = TranscriptReducer()
    assert _kinds(r.feed(MessageBlockStarted())) == ["open_block"]
    assert _kinds(r.feed(MessageBlockDelta(text="hi"))) == ["append_delta"]
    ops = r.feed(MessageBlockCompleted(markdown="hi", streamed=True))
    assert _kinds(ops) == ["close_block"]
    assert isinstance(ops[0], CloseBlock) and ops[0].streamed is True


def test_user_completion_is_user_message_and_cached_for_compaction():
    r = TranscriptReducer()
    ops = r.feed(MessageBlockCompleted(markdown="fix the bug", role="user"))
    assert _kinds(ops) == ["render_user_message"]
    assert isinstance(ops[0], RenderUserMessage) and ops[0].markdown == "fix the bug"
    # The cached prompt rides on the next compaction op.
    comp = r.feed(ConversationCompacted(summary="s", message_count=5))
    assert isinstance(comp[0], ClearForCompaction)
    assert comp[0].last_user_prompt == "fix the bug"
    assert comp[0].message_count == 5


def test_user_message_threads_message_id_as_delete_anchor():
    """The user completion carries the backend ``Message.id`` through to the op so
    the Textual host can anchor a react-unit delete on it. ``surface_args`` includes
    the id (couples the arity across every surface)."""
    r = TranscriptReducer()
    ops = r.feed(MessageBlockCompleted(markdown="fix the bug", role="user", message_id="msg-42"))
    assert _kinds(ops) == ["render_user_message"]
    op = ops[0]
    assert isinstance(op, RenderUserMessage)
    assert op.message_id == "msg-42"
    assert op.surface_args() == ("fix the bug", "msg-42")


def test_user_message_without_id_threads_none():
    r = TranscriptReducer()
    ops = r.feed(MessageBlockCompleted(markdown="hello", role="user"))
    op = ops[0]
    assert isinstance(op, RenderUserMessage)
    assert op.message_id is None
    assert op.surface_args() == ("hello", None)


# ------------------------------------------------------------- thinking ---
def test_reasoning_delta_opens_thinking_once_then_clears():
    r = TranscriptReducer()
    ops = r.feed(ReasoningDelta(text="pondering"))
    assert _kinds(ops) == ["set_thinking", "append_delta"]
    assert isinstance(ops[0], SetThinking) and ops[0].on is True
    assert isinstance(ops[1], AppendDelta) and ops[1].reasoning is True
    # A second reasoning token does NOT re-open thinking.
    assert _kinds(r.feed(ReasoningDelta(text="more"))) == ["append_delta"]
    # A visible (non-transparent) event ends thinking.
    ops = r.feed(MessageBlockDelta(text="answer"))
    assert _kinds(ops) == ["set_thinking", "append_delta"]
    assert ops[0].on is False


def test_usage_is_thinking_transparent():
    r = TranscriptReducer()
    r.feed(ReasoningDelta(text="x"))
    # A status-only usage event does not end the thinking state.
    assert _kinds(r.feed(UsageUpdated(model="m"))) == ["update_usage"]


# ---------------------------------------------------------------- group ---
def test_consecutive_search_read_coalesce():
    r = TranscriptReducer()
    assert _kinds(r.feed(_started("Read", "t1", "/a.py"))) == ["open_group", "add_to_group"]
    assert _kinds(r.feed(_started("Grep", "t2", "foo"))) == ["add_to_group"]
    assert _kinds(r.feed(_started("Glob", "t3", "*.py"))) == ["add_to_group"]
    # Completions fold into the group (no standalone rows).
    assert _kinds(r.feed(_completed("Read", "t1"))) == ["complete_in_group"]
    assert _kinds(r.feed(_completed("Grep", "t2"))) == ["complete_in_group"]


def test_noncollapsible_tool_flushes_group_then_stands_alone():
    r = TranscriptReducer()
    r.feed(_started("Read", "t1", "/a.py"))
    ops = r.feed(_started("Write", "t2", "/b.py"))
    assert _kinds(ops) == ["flush_group", "tool_started"]
    assert isinstance(ops[1], ToolStarted) and ops[1].fold is FoldMode.NONE


def test_assistant_text_breaks_group_then_next_tool_opens_new_group():
    r = TranscriptReducer()
    r.feed(_started("Read", "t1", "/a.py"))
    # Assistant text is non-transparent → the front-of-feed guard flushes.
    ops = r.feed(MessageBlockCompleted(markdown="reply", streamed=False))
    assert _kinds(ops) == ["flush_group", "close_block"]
    # The next grouped tool opens a fresh group.
    assert _kinds(r.feed(_started("Grep", "t2", "foo"))) == ["open_group", "add_to_group"]


def test_detail_tool_started_carries_detail_fold():
    r = TranscriptReducer()
    ops = r.feed(_started("Bash", "b1", "ls"))
    assert _kinds(ops) == ["tool_started"]
    assert ops[0].fold is FoldMode.DETAIL


def test_grouped_completion_survives_an_interrupting_flush():
    """A grouped tool that completes AFTER its run was broken still folds in.

    a tool may finish after an interrupting event flushed its group;
    the reducer keeps the grouped id until its completion arrives.
    """
    r = TranscriptReducer()
    r.feed(_started("Read", "t1", "/a.py"))  # open + add
    # An error breaks the run (flush) — but t1 hasn't completed yet.
    ops = r.feed(ErrorRaised(text="boom"))
    assert _kinds(ops) == ["flush_group", "render_error"]
    # t1's late completion STILL folds into its (now-flushed) group.
    assert _kinds(r.feed(_completed("Read", "t1"))) == ["complete_in_group"]


def test_standalone_completion_without_started_is_tool_completed():
    r = TranscriptReducer()
    ops = r.feed(_completed("Bash", "b1"))
    assert _kinds(ops) == ["tool_completed"]
    assert isinstance(ops[0], ToolCompleted)


# ---------------------------------------------------------------- retry ---
def test_retry_then_any_event_clears_it_first():
    r = TranscriptReducer()
    ops = r.feed(RetryStatus(attempt=2, max_attempts=6, delay_ms=2000.0))
    assert _kinds(ops) == ["set_retry"]
    assert isinstance(ops[0], SetRetry)
    # The next event prepends a ClearRetry (the countdown never persists).
    ops = r.feed(Notice(text="ok"))
    assert _kinds(ops) == ["clear_retry", "render_notice"]
    assert isinstance(ops[0], ClearRetry) and isinstance(ops[1], RenderNotice)


def test_retry_is_group_and_thinking_transparent():
    r = TranscriptReducer()
    r.feed(_started("Read", "t1", "/a.py"))  # open a group
    r.feed(ReasoningDelta(text="x"))  # NOTE: reasoning is non-group → flush?
    # ``reasoning_delta`` is NOT group-transparent, so it flushed the group; open
    # a fresh one to test retry transparency in isolation.
    r2 = TranscriptReducer()
    r2.feed(_started("Read", "z1", "/a.py"))
    ops = r2.feed(RetryStatus(attempt=1, max_attempts=6))
    # retry does not flush the open group nor end anything.
    assert _kinds(ops) == ["set_retry"]


# --------------------------------------------------------- boundaries ----
def test_compaction_resets_group_and_block_state():
    r = TranscriptReducer()
    r.feed(_started("Read", "t1", "/a.py"))  # open a group
    ops = r.feed(ConversationCompacted(summary="recap", message_count=3))
    # The group is flushed by the front guard, then the compaction op fires.
    assert _kinds(ops) == ["flush_group", "clear_for_compaction"]
    assert isinstance(ops[1], ClearForCompaction)
    assert ops[1].summary == "recap" and ops[1].message_count == 3
    # State reset: a following grouped tool opens a brand-new group.
    assert _kinds(r.feed(_started("Grep", "t2", "foo"))) == ["open_group", "add_to_group"]


def test_transcript_cleared_resets_state():
    r = TranscriptReducer()
    r.feed(_started("Read", "t1", "/a.py"))
    ops = r.feed(TranscriptCleared())
    assert _kinds(ops) == ["flush_group", "clear_transcript"]
    assert isinstance(ops[1], ClearTranscript)


def test_usage_does_not_break_open_group():
    r = TranscriptReducer()
    r.feed(_started("Read", "t1", "/a.py"))
    assert _kinds(r.feed(UsageUpdated(model="m"))) == ["update_usage"]
    # The group is still open — a second grouped call keeps coalescing.
    assert _kinds(r.feed(_started("Grep", "t2", "foo"))) == ["add_to_group"]
    assert isinstance(r.feed(UsageUpdated(model="m"))[0], UpdateUsage)


# ------------------------------------------------------ activity scope ----
# An activity opens at its own scope ``(graph,)``; every later CHILD event (a
# tool dispatched inside a node, a progress ping) carries the LONGER child scope
# ``(graph, node)``. The reducer must route each op back to the OWNING activity's
# scope — the exact key the surface stored the widget under (``open_activity``) —
# else the surface ``_activity_widgets.get(scope)`` lookup misses and the live
# node trail + folded child tool rows silently vanish. These lock that routing.
_GRAPH = ("graph:7f",)
_NODE = ("graph:7f", "node:a")


def _activity_started(scope=_GRAPH, kind="graph", label="run_graph"):
    return ActivityStarted(scope=scope, activity_kind=kind, label=label, topology={"nodes": [], "edges": []})


def test_activity_opens_keyed_by_its_own_scope():
    r = TranscriptReducer()
    ops = r.feed(_activity_started())
    assert _kinds(ops) == ["open_activity"]
    op = ops[0]
    assert isinstance(op, OpenActivity)
    assert op.scope == _GRAPH


def test_child_tool_started_routes_to_owning_activity_scope():
    r = TranscriptReducer()
    r.feed(_activity_started())
    # A tool dispatched INSIDE the graph carries the longer (graph, node) scope.
    child = ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id=None, scope=_NODE)
    ops = r.feed(child)
    assert _kinds(ops) == ["add_activity_tool_call"]
    op = ops[0]
    assert isinstance(op, AddActivityToolCall)
    # The op keys by the OWNING activity's scope (graph,), NOT the child's (graph, node).
    assert op.scope == _GRAPH


def test_child_tool_completed_routes_to_owning_activity_scope():
    r = TranscriptReducer()
    r.feed(_activity_started())
    done = ToolCallCompleted(tool_name="Bash", tool_use_id=None, summary="ok", scope=_NODE)
    ops = r.feed(done)
    assert _kinds(ops) == ["complete_activity_tool_call"]
    op = ops[0]
    assert isinstance(op, CompleteActivityToolCall)
    assert op.scope == _GRAPH


def test_scoped_progress_ping_routes_to_owning_activity_scope():
    r = TranscriptReducer()
    r.feed(_activity_started())
    ping = TaskProgress(scope=_NODE, stage="node:a", status="running", detail="step 1")
    ops = r.feed(ping)
    assert _kinds(ops) == ["update_activity_node"]
    op = ops[0]
    assert isinstance(op, UpdateActivityNode)
    assert op.scope == _GRAPH
    assert op.stage == "node:a" and op.status == "running" and op.detail == "step 1"


def test_activity_completed_keyed_by_its_own_scope():
    r = TranscriptReducer()
    r.feed(_activity_started())
    ops = r.feed(ActivityCompleted(scope=_GRAPH, outcome="success", node_states=[], summary="done"))
    assert _kinds(ops) == ["close_activity"]
    op = ops[0]
    assert isinstance(op, CloseActivity)
    assert op.scope == _GRAPH


def test_unscoped_progress_ping_stays_top_level():
    """A background task's ping (no matching open activity) is NOT swallowed by an
    activity — it renders as a standalone top-level row."""
    r = TranscriptReducer()
    r.feed(_activity_started())
    # An unscoped ping owns to no open activity → top-level TaskProgress row.
    ops = r.feed(TaskProgress(scope=(), stage="bg", status="running"))
    assert _kinds(ops) == ["render_task_progress"]
    assert isinstance(ops[0], RenderTaskProgress)


def test_child_of_unmatched_scope_does_not_route_to_activity():
    """A tool scoped under a DIFFERENT graph than the open one owns to nothing and
    stands alone (its scope is not a suffix of the open activity's)."""
    r = TranscriptReducer()
    r.feed(_activity_started(scope=("graph:aa",)))
    other = ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id="b1", scope=("graph:bb", "node:x"))
    ops = r.feed(other)
    # No owning activity → a standalone DETAIL tool row (Bash), not add_activity_tool_call.
    assert "add_activity_tool_call" not in _kinds(ops)
    assert _kinds(ops) == ["tool_started"]


def test_nested_activities_route_to_innermost_owner():
    """Two nested activities open at (graph,) and (graph, sub); a child under
    (graph, sub, node) routes to the LONGEST matching prefix (the inner one)."""
    r = TranscriptReducer()
    outer = ("graph:o",)
    inner = ("graph:o", "sub:i")
    r.feed(_activity_started(scope=outer))
    r.feed(_activity_started(scope=inner, kind="agent", label="sub"))
    child = ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id=None, scope=("graph:o", "sub:i", "node:n"))
    ops = r.feed(child)
    assert _kinds(ops) == ["add_activity_tool_call"]
    assert ops[0].scope == inner  # longest-prefix wins → innermost activity


def test_closed_activity_no_longer_owns_child_events():
    """After close_activity drops the scope, a late child ping no longer routes to
    it (the widget handle is gone) — it falls through to a top-level row."""
    r = TranscriptReducer()
    r.feed(_activity_started())
    r.feed(ActivityCompleted(scope=_GRAPH, outcome="success", node_states=[], summary="done"))
    ops = r.feed(TaskProgress(scope=_NODE, stage="node:a", status="running"))
    assert _kinds(ops) == ["render_task_progress"]
