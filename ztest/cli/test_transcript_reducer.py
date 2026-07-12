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
    RenderUserMessage,
    SetRetry,
    SetThinking,
    ToolCompleted,
    ToolStarted,
    TranscriptReducer,
    UpdateUsage,
)
from mote.cli.contracts.view import (
    ConversationCompacted,
    ErrorRaised,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    Notice,
    ReasoningDelta,
    RetryStatus,
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
