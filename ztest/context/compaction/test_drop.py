#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the DESTRUCTIVE :class:`HeadDropReducer` — last-resort head drop.

Drops the oldest non-pinned segments (whole segments only, so pairing is never
broken) and prepends a truncation marker. Gated to ``HARD`` urgency; pinned
segments and at least the most-recent non-pinned segment are always kept.
"""
from __future__ import annotations

import asyncio

from mote.contracts.constants.context import HEAD_DROPPED_MESSAGE
from mote.contracts.schema import ContextManagerConfig, Message
from mote.runtime.context.compaction.reducers.drop import HeadDropReducer
from mote.runtime.context.compaction.request import ReductionReason, ReductionRequest, Urgency
from mote.runtime.context.compaction.transcript import Transcript

from ..conftest import make_pairs, text_msg


def _run(coro):
    return asyncio.run(coro)


def _reduce(transcript, *, target, urgency=Urgency.HARD):
    reducer = HeadDropReducer(ContextManagerConfig(), model="gpt-4")
    req = ReductionRequest(target_tokens=target, urgency=urgency, reason=ReductionReason.REACTIVE)
    return _run(reducer.reduce(transcript, req))


def _assert_pairing_valid(messages):
    seen: set = set()
    for m in messages:
        calls = m.metadata.get("tool_calls")
        if calls:
            for c in calls:
                seen.add(c["id"])
        cid = m.metadata.get("tool_call_id")
        if cid is not None:
            assert cid in seen, f"orphan tool_result {cid} before its call"


def _big_history():
    return Transcript.from_messages([text_msg(("word " * 100) + f" {i}") for i in range(8)])


def test_soft_request_is_noop():
    t = _big_history()
    out = _reduce(t, target=1, urgency=Urgency.SOFT)
    assert out.changed is False
    assert out.strategy == "head_drop"


def test_already_under_target_is_noop():
    t = _big_history()
    out = _reduce(t, target=10_000_000)
    assert out.changed is False
    assert out.target_met is True


def test_drops_oldest_and_prepends_marker():
    t = _big_history()
    pre = t.token_count("gpt-4")
    out = _reduce(t, target=pre // 3)
    assert out.changed is True
    msgs = out.transcript.to_messages()
    assert msgs[0].content == HEAD_DROPPED_MESSAGE
    assert out.transcript.token_count("gpt-4") < pre
    assert out.tokens_freed > 0


def test_keeps_at_least_last_segment():
    t = _big_history()
    # An unreachable target forces dropping everything droppable, but the most
    # recent non-pinned segment must still survive.
    out = _reduce(t, target=1)
    msgs = out.transcript.to_messages()
    # marker + at least one real message.
    assert len(msgs) >= 2
    assert msgs[0].content == HEAD_DROPPED_MESSAGE


def test_pinned_system_anchor_survives():
    sys = Message(content="SYSTEM RULES", role="system")
    t = Transcript.from_messages([sys, *[text_msg(("word " * 100) + f" {i}") for i in range(8)]])
    pre = t.token_count("gpt-4")
    out = _reduce(t, target=pre // 3)
    contents = [m.content for m in out.transcript.to_messages()]
    assert "SYSTEM RULES" in contents


def test_drop_never_breaks_tool_pairing():
    msgs = make_pairs(8, result="y" * 400)
    t = Transcript.from_messages(msgs)
    pre = t.token_count("gpt-4")
    out = _reduce(t, target=pre // 3)
    assert out.changed is True
    _assert_pairing_valid(out.transcript.to_messages())
