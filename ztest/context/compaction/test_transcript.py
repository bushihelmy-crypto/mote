#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the L0 :class:`Transcript` — the boundary-safe structured history.

The whole reason this layer exists is to make the tool_call↔tool_result pairing
atomic. The critical assertion here is the boundary-safe split: no matter where
the tail floors land, the split never bisects a ``TOOL_GROUP`` — so summarize can
never produce an orphan ``tool_result`` (the old flat-index bug that 400'd
Anthropic).
"""
from __future__ import annotations

from metagpt.common.schema import Message
from metagpt.context.compaction.transcript import (
    SegmentKind,
    Transcript,
)

from ..conftest import COMPACTABLE, make_pairs, text_msg, tool_call_msg, tool_pair, tool_result_msg


# ---------------------------------------------------------------------------
# Segmentation / grouping
# ---------------------------------------------------------------------------


def test_plain_messages_are_message_segments():
    t = Transcript.from_messages([text_msg("a"), text_msg("b", role="assistant")])
    assert [s.kind for s in t.segments] == [SegmentKind.MESSAGE, SegmentKind.MESSAGE]
    assert t.message_count() == 2


def test_tool_call_and_results_form_one_atomic_group():
    msgs = tool_pair("id-0", "Read", "content")
    t = Transcript.from_messages(msgs, compactable=COMPACTABLE)
    assert len(t.segments) == 1
    seg = t.segments[0]
    assert seg.kind == SegmentKind.TOOL_GROUP
    assert len(seg.messages) == 2  # the assistant call + its one result
    assert seg.reconstructable is True  # Read is compactable


def test_tool_group_with_multiple_results():
    call = tool_call_msg("multi", "Read")
    call.metadata["tool_calls"] = [
        {"id": "a", "name": "Read", "args": {}},
        {"id": "b", "name": "Grep", "args": {}},
    ]
    r1 = tool_result_msg("a", "res-a")
    r2 = tool_result_msg("b", "res-b")
    t = Transcript.from_messages([call, r1, r2], compactable=COMPACTABLE)
    assert len(t.segments) == 1
    assert len(t.segments[0].messages) == 3
    assert t.segments[0].reconstructable is True


def test_non_compactable_tool_group_not_reconstructable():
    call = tool_call_msg("q", "AskUserQuestion")
    res = tool_result_msg("q", "the answer")
    # AskUserQuestion is absent from the injected compactable set → not foldable.
    t = Transcript.from_messages([call, res], compactable=COMPACTABLE)
    assert t.segments[0].kind == SegmentKind.TOOL_GROUP
    assert t.segments[0].reconstructable is False


def test_system_messages_are_pinned_anchors():
    sys = Message(content="system rules", role="system")
    t = Transcript.from_messages([sys, text_msg("hi")])
    assert t.segments[0].kind == SegmentKind.SYSTEM_ANCHOR
    assert t.segments[0].pinned is True
    assert t.segments[1].pinned is False


def test_retention_pin_makes_a_tool_group_pinned():
    # A tool result tagged RETENTION_PIN promotes its whole group to pinned, so
    # summarize / head-drop (which key off Segment.pinned) leave it verbatim —
    # the same protection a SYSTEM_ANCHOR gets, granted per-result via metadata.
    from metagpt.common.const import RETENTION, RETENTION_PIN

    call, result = tool_pair("id-0", "Read", "precious")
    result.add_metadata(RETENTION, RETENTION_PIN)
    t = Transcript.from_messages([call, result], compactable=COMPACTABLE)
    assert t.segments[0].kind == SegmentKind.TOOL_GROUP
    assert t.segments[0].pinned is True


def test_orphan_tool_result_falls_through_to_message():
    # A tool result with no preceding matching call is a plain MESSAGE (the
    # transcript groups what is groupable; it does not invent a partner).
    orphan = tool_result_msg("nope", "stray")
    t = Transcript.from_messages([orphan])
    assert t.segments[0].kind == SegmentKind.MESSAGE


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_roundtrip_preserves_order_and_identity():
    msgs = [text_msg("u1"), *tool_pair("id-0", "Read", "r0"), text_msg("a1", role="assistant")]
    t = Transcript.from_messages(msgs)
    back = t.to_messages()
    assert back == msgs  # same objects, same order
    assert t.messages == msgs


# ---------------------------------------------------------------------------
# Boundary-safe split — the core regression guard
# ---------------------------------------------------------------------------


def test_split_keeps_all_when_history_too_short():
    t = Transcript.from_messages([text_msg("a"), text_msg("b")])
    split = t.split_keep_tail(keep_tail_messages=5, keep_tail_tokens=1, model="gpt-4")
    assert split == len(t.segments)  # nothing to summarize


def test_split_never_bisects_a_tool_group():
    # Build history where a tool group straddles the natural per-message cut. The
    # old flat splitter could land inside the (call, result) pair; the segment
    # splitter must return a boundary that keeps the group whole.
    msgs = [text_msg(f"turn {i}") for i in range(3)]
    msgs += make_pairs(6, result="y" * 400)  # 12 messages, 6 atomic groups
    t = Transcript.from_messages(msgs)

    split = t.split_keep_tail(keep_tail_messages=3, keep_tail_tokens=1, model="gpt-4")
    head = Transcript(t.segments[:split])
    tail = Transcript(t.segments[split:])

    # The tail's first message must NOT be an orphan tool result: either a plain
    # message or the assistant head of a tool group.
    tail_msgs = tail.to_messages()
    assert tail_msgs, "expected a non-empty tail"
    first = tail_msgs[0]
    assert first.metadata.get("tool_call_id") is None, "tail starts on an orphan tool_result!"

    # And every tool group is fully on one side of the cut.
    for seg in t.segments:
        if seg.kind == SegmentKind.TOOL_GROUP:
            in_head = seg in head.segments
            in_tail = seg in tail.segments
            assert in_head ^ in_tail


def test_split_leaves_at_least_one_head_segment():
    msgs = [text_msg(f"m{i} content here") for i in range(8)]
    t = Transcript.from_messages(msgs)
    # keep_tail_messages small enough to clear the "too short" guard, but an
    # unreachable token floor forces the backward walk to consume everything.
    split = t.split_keep_tail(keep_tail_messages=2, keep_tail_tokens=10_000_000, model="gpt-4")
    assert split == 1  # forced to keep at least one head segment


# ---------------------------------------------------------------------------
# drop / replace_range
# ---------------------------------------------------------------------------


def test_drop_removes_segments():
    t = Transcript.from_messages([text_msg("a"), text_msg("b"), text_msg("c")])
    dropped = t.drop([0, 2])
    assert [m.content for m in dropped.to_messages()] == ["b"]
    # original untouched (new transcript returned)
    assert t.message_count() == 3


def test_replace_range_splices_replacement():
    t = Transcript.from_messages([text_msg("a"), text_msg("b"), text_msg("c")])
    out = t.replace_range(0, 2, [text_msg("summary")])
    assert [m.content for m in out.to_messages()] == ["summary", "c"]


def test_first_unpinned_index_skips_system():
    sys = Message(content="rules", role="system")
    t = Transcript.from_messages([sys, text_msg("first real")])
    assert t.first_unpinned_index() == 1
