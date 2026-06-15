#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``ReActLoop._observe`` — the buffer → filter → commit pipeline.

The loop owns observe: it pops from ``ctx.msg_buffer``, keeps the messages whose
``cause_by`` is watched OR addressed to ``ctx.name`` (deduped against stored
history when ``enable_memory``), commits to the memory store (all news when
``observe_all`` else only the filtered set) and tracks ``latest_observed_msg``.
"""
from __future__ import annotations

import pytest

from metagpt.common.schema import CauseBy, MessagePriority, Message, UserMessage

from .conftest import make_loop_context


def _msg(content="m", *, cause_by="", send_to=None) -> Message:
    kwargs = {"cause_by": cause_by}
    if send_to is not None:
        kwargs["send_to"] = send_to
    return UserMessage(content, **kwargs)


@pytest.mark.asyncio
async def test_observe_no_buffer_returns_zero(make_loop):
    b = make_loop(msg_buffer=None)
    b.loop._ctx = b.ctx
    assert await b.loop._observe() == 0


@pytest.mark.asyncio
async def test_observe_empty_buffer_returns_zero(make_loop):
    b = make_loop()
    b.loop._ctx = b.ctx
    assert await b.loop._observe() == 0


@pytest.mark.asyncio
async def test_observe_keeps_watched_cause_by(make_loop):
    # watch set matches the message's cause_by -> kept even if not addressed.
    b = make_loop(watch={CauseBy.RUN_COMMAND.value}, name="Alice")
    b.loop._ctx = b.ctx
    kept = _msg("watched", cause_by=CauseBy.RUN_COMMAND, send_to={"Someone"})
    dropped = _msg("ignored", cause_by=CauseBy.ACTION, send_to={"Someone"})
    b.buffer.push(kept)
    b.buffer.push(dropped)

    assert await b.loop._observe() == 1
    assert b.loop.latest_observed_msg is kept


@pytest.mark.asyncio
async def test_observe_keeps_addressed_to_name(make_loop):
    # Not watched, but addressed to ctx.name -> kept.
    b = make_loop(watch=set(), name="Alice")
    b.loop._ctx = b.ctx
    addressed = _msg("for-alice", cause_by=CauseBy.ACTION, send_to={"Alice"})
    b.buffer.push(addressed)

    assert await b.loop._observe() == 1
    assert b.loop.latest_observed_msg is addressed


@pytest.mark.asyncio
async def test_observe_dedup_against_stored_history(make_loop):
    # Same object already in memory -> filtered out (enable_memory=True).
    b = make_loop(watch=set(), name="Alice", enable_memory=True)
    b.loop._ctx = b.ctx
    seen = _msg("dup", send_to={"Alice"})
    await b.memory.add(seen)
    b.buffer.push(seen)

    assert await b.loop._observe() == 0
    assert b.loop.latest_observed_msg is None


@pytest.mark.asyncio
async def test_observe_no_dedup_when_memory_disabled(make_loop):
    # enable_memory=False -> old_messages is [] so the dup is NOT filtered.
    b = make_loop(watch=set(), name="Alice", enable_memory=False)
    b.loop._ctx = b.ctx
    seen = _msg("dup", send_to={"Alice"})
    await b.memory.add(seen)
    b.buffer.push(seen)

    assert await b.loop._observe() == 1


@pytest.mark.asyncio
async def test_observe_all_commits_every_news(make_loop):
    # observe_all=True -> add_batch receives ALL popped messages, not just filtered.
    b = make_loop(watch=set(), name="Alice", observe_all=True)
    b.loop._ctx = b.ctx
    kept = _msg("kept", send_to={"Alice"})
    other = _msg("other", send_to={"Bob"})
    b.buffer.push(kept)
    b.buffer.push(other)

    news = await b.loop._observe()
    assert news == 1  # return value is the filtered count
    assert b.memory.add_batch_calls[-1] == [kept, other]  # but ALL were committed


@pytest.mark.asyncio
async def test_observe_filtered_only_when_not_observe_all(make_loop):
    b = make_loop(watch=set(), name="Alice", observe_all=False)
    b.loop._ctx = b.ctx
    kept = _msg("kept", send_to={"Alice"})
    other = _msg("other", send_to={"Bob"})
    b.buffer.push(kept)
    b.buffer.push(other)

    assert await b.loop._observe() == 1
    assert b.memory.add_batch_calls[-1] == [kept]


@pytest.mark.asyncio
async def test_observe_latest_is_none_when_nothing_passes(make_loop):
    b = make_loop(watch=set(), name="Alice", observe_all=True)
    b.loop._ctx = b.ctx
    b.buffer.push(_msg("other", send_to={"Bob"}))

    assert await b.loop._observe() == 0
    assert b.loop.latest_observed_msg is None


@pytest.mark.asyncio
async def test_observe_respects_max_priority(make_loop):
    # A LATER-priority message is invisible when only NEXT is requested.
    b = make_loop(watch=set(), name="Alice")
    b.loop._ctx = b.ctx
    later = _msg("later", send_to={"Alice"})
    b.buffer.push(later, priority=MessagePriority.LATER)

    assert await b.loop._observe(max_priority=MessagePriority.NEXT) == 0
    # Pops once the bar is raised to LATER.
    assert await b.loop._observe(max_priority=MessagePriority.LATER) == 1
