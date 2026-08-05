#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``ObservationService`` — the admitted inbox → history pipeline.

The routing boundary admits messages before they reach ``ctx.msg_buffer``. The
loop pops that private inbox, deduplicates against stored history, commits the
new messages, and tracks ``latest_observed_msg``.
"""

from __future__ import annotations

import pytest

from mote.contracts.conversation import CauseBy, Message, MessagePriority, UserMessage
from mote.contracts.conversation.fields import INTERJECTION

from .conftest import make_flow_context


def _msg(content="m", *, cause_by="", send_to=None) -> Message:
    kwargs = {"cause_by": cause_by}
    if send_to is not None:
        kwargs["send_to"] = send_to
    return UserMessage(content, **kwargs)


@pytest.mark.asyncio
async def test_observe_no_buffer_returns_zero(make_engine):
    b = make_engine(msg_buffer=None)
    b.engine._ctx = b.ctx
    assert (await b.engine._observation.observe()).observed_count == 0


@pytest.mark.asyncio
async def test_observe_empty_buffer_returns_zero(make_engine):
    b = make_engine()
    b.engine._ctx = b.ctx
    assert (await b.engine._observation.observe()).observed_count == 0


@pytest.mark.asyncio
async def test_observe_accepts_every_admitted_inbox_message(make_engine):
    b = make_engine(watch={CauseBy.RUN_COMMAND.value}, name="Alice")
    b.engine._ctx = b.ctx
    watched = _msg("watched", cause_by=CauseBy.RUN_COMMAND, send_to={"Someone"})
    unwatched = _msg("unwatched", cause_by=CauseBy.ACTION, send_to={"Someone"})
    b.buffer.push(watched)
    b.buffer.push(unwatched)

    assert (await b.engine._observation.observe()).observed_count == 2
    assert b.memory.add_batch_calls[-1] == [watched, unwatched]
    assert b.engine.latest_observed_msg is unwatched


@pytest.mark.asyncio
async def test_observe_does_not_recheck_recipient(make_engine):
    b = make_engine(watch=set(), name="Alice")
    b.engine._ctx = b.ctx
    admitted = _msg("already-admitted", cause_by=CauseBy.ACTION, send_to={"Bob"})
    b.buffer.push(admitted)

    assert (await b.engine._observation.observe()).observed_count == 1
    assert b.engine.latest_observed_msg is admitted


@pytest.mark.asyncio
async def test_observe_dedup_against_stored_history(make_engine):
    # Same object already in memory -> filtered out (enable_memory=True).
    b = make_engine(watch=set(), name="Alice", enable_memory=True)
    b.engine._ctx = b.ctx
    seen = _msg("dup", send_to={"Alice"})
    await b.memory.add(seen)
    b.buffer.push(seen)

    assert (await b.engine._observation.observe()).observed_count == 0
    assert b.engine.latest_observed_msg is None


@pytest.mark.asyncio
async def test_observe_no_dedup_when_memory_disabled(make_engine):
    # enable_memory=False -> old_messages is [] so the dup is NOT filtered.
    b = make_engine(watch=set(), name="Alice", enable_memory=False)
    b.engine._ctx = b.ctx
    seen = _msg("dup", send_to={"Alice"})
    await b.memory.add(seen)
    b.buffer.push(seen)

    assert (await b.engine._observation.observe()).observed_count == 1


@pytest.mark.asyncio
async def test_observe_respects_max_priority(make_engine):
    # A NEXT-priority message is invisible when only NOW is requested.
    b = make_engine(watch=set(), name="Alice")
    b.engine._ctx = b.ctx
    later = _msg("later", send_to={"Alice"})
    b.buffer.push(later, priority=MessagePriority.NEXT)

    assert (await b.engine._observation.observe(max_priority=MessagePriority.NOW)).observed_count == 0
    # Pops once the bar is raised to NEXT.
    assert (await b.engine._observation.observe(max_priority=MessagePriority.NEXT)).observed_count == 1


# ---------------------------------------------------------------------------
# Interjection framing — a user message drained mid-turn (interjection=True)
# is wrapped so the model can tell it apart from the turn's original prompt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_frames_mid_turn_user_message(make_engine):
    b = make_engine(watch=set(), name="Alice")
    b.engine._ctx = b.ctx
    steer = _msg("stop and do X", send_to={"Alice"})
    b.buffer.push(steer)

    assert (await b.engine._observation.observe(interjection=True)).observed_count == 1
    assert "The user sent a message while you were working:" in steer.content
    assert "<user_query>\nstop and do X\n</user_query>" in steer.content
    assert steer.metadata[INTERJECTION] is True


@pytest.mark.asyncio
async def test_observe_initial_does_not_frame(make_engine):
    # The turn's first prompt (default interjection=False) is left verbatim.
    b = make_engine(watch=set(), name="Alice")
    b.engine._ctx = b.ctx
    prompt = _msg("original prompt", send_to={"Alice"})
    b.buffer.push(prompt)

    assert (await b.engine._observation.observe()).observed_count == 1
    assert prompt.content == "original prompt"
    assert INTERJECTION not in prompt.metadata


@pytest.mark.asyncio
async def test_observe_frame_is_idempotent(make_engine):
    # An already-framed message (metadata flag set) is not double-wrapped.
    b = make_engine(watch=set(), name="Alice")
    b.engine._ctx = b.ctx
    already = _msg("already", send_to={"Alice"})
    already.metadata[INTERJECTION] = True
    b.buffer.push(already)

    assert (await b.engine._observation.observe(interjection=True)).observed_count == 1
    assert already.content == "already"


@pytest.mark.asyncio
async def test_observe_does_not_frame_non_user_message(make_engine):
    # A bg-task notification / tool result flows through untouched.
    b = make_engine(watch=set(), name="Alice")
    b.engine._ctx = b.ctx
    note = Message("bg task done", role="tool", send_to={"Alice"})
    b.buffer.push(note)

    assert (await b.engine._observation.observe(interjection=True)).observed_count == 1
    assert note.content == "bg task done"
    assert INTERJECTION not in note.metadata
