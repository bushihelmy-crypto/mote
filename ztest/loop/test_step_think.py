#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``ReActLoop._step_think`` — the single think primitive.

Returns ``False`` immediately when the shared ``active`` signal is off (the
``End`` tool / ask_user "stop" path); otherwise it asks the context provider to
``prepare()`` the request, lazily ``resolve_llm()`` and hands everything to
``think_engine.start()``, returning ``True``.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_step_think_returns_false_when_inactive(make_loop):
    b = make_loop(active=False)
    b.loop._ctx = b.ctx

    assert await b.loop._step_think() is False
    # Nothing was prepared / started.
    assert b.provider.prepare_calls == 0
    assert b.think_engine.start_calls == []


async def test_step_think_prepares_and_starts(make_loop):
    b = make_loop(active=True)
    b.loop._ctx = b.ctx

    assert await b.loop._step_think() is True
    assert b.provider.prepare_calls == 1
    # The resolved LLM was driven off the prepared request's messages.
    assert b.provider.resolve_calls == [b.provider._think_request.req]

    call = b.think_engine.start_calls[0]
    tr = b.provider._think_request
    assert call["req"] is tr.req
    assert call["system_prompt"] == tr.system_prompt
    assert call["tool_specs"] == tr.tool_specs
    assert call["llm"] is b.provider.llm


# ---------------------------------------------------------------------------
# Persistent turn-context recording (was Role._record_turn_context).
#
# The persistent (save_to_context) bucket is committed to history by the loop
# on each think cycle, right before prepare(). Committing it here — after
# _observe has already committed the turn's user prompt — is what keeps history
# (and the durable rollout) in prompt -> turn-context order, symmetric with the
# ephemeral bucket that PromptBuilder appends to the user prompt.
# ---------------------------------------------------------------------------


class _FakeTurnContextBus:
    """Minimal bus exposing only the persisted-bucket entry point."""

    def __init__(self, block: str):
        self._block = block
        self.seen_cwd = "unset"

    async def collect_to_context(self, *, cwd=None):
        self.seen_cwd = cwd
        return self._block


async def test_step_think_records_non_empty_turn_context_to_memory(make_loop):
    bus = _FakeTurnContextBus("<system-reminder>\ngit changed\n</system-reminder>")
    b = make_loop(active=True, turn_context_bus=bus)
    b.loop._ctx = b.ctx

    await b.loop._step_think()

    # The persisted block landed in history as a user message.
    added = [m for m in b.memory.messages if m.content == "<system-reminder>\ngit changed\n</system-reminder>"]
    assert len(added) == 1
    assert added[0].role == "user"


async def test_step_think_empty_turn_context_adds_nothing(make_loop):
    bus = _FakeTurnContextBus("")
    b = make_loop(active=True, turn_context_bus=bus)
    b.loop._ctx = b.ctx
    before = len(b.memory.messages)

    await b.loop._step_think()

    assert len(b.memory.messages) == before


async def test_step_think_passes_live_cwd_to_turn_context_bus(make_loop):
    bus = _FakeTurnContextBus("")
    b = make_loop(active=True, turn_context_bus=bus, get_cwd=lambda: "/some/dir")
    b.loop._ctx = b.ctx

    await b.loop._step_think()

    assert bus.seen_cwd == "/some/dir"


async def test_step_think_no_bus_records_nothing(make_loop):
    # turn_context_bus defaults to None — recording is a no-op, think still runs.
    b = make_loop(active=True)
    b.loop._ctx = b.ctx
    before = len(b.memory.messages)

    assert await b.loop._step_think() is True
    assert len(b.memory.messages) == before


async def test_step_think_records_turn_context_before_prepare(make_loop):
    """The block is in history by the time prepare() builds the request, so the
    turn-context is visible to this cycle's think (order: commit -> prepare)."""
    bus = _FakeTurnContextBus("<system-reminder>\nstate\n</system-reminder>")
    b = make_loop(active=True, turn_context_bus=bus)
    b.loop._ctx = b.ctx

    seen_at_prepare: list[int] = []
    orig_prepare = b.provider.prepare

    async def spy_prepare():
        seen_at_prepare.append(len(b.memory.messages))
        return await orig_prepare()

    b.provider.prepare = spy_prepare

    await b.loop._step_think()

    # At least one message (the recorded block) was already committed when
    # prepare() ran.
    assert seen_at_prepare and seen_at_prepare[0] >= 1


async def test_step_think_inactive_skips_turn_context_recording(make_loop):
    bus = _FakeTurnContextBus("<system-reminder>\nx\n</system-reminder>")
    b = make_loop(active=False, turn_context_bus=bus)
    b.loop._ctx = b.ctx
    before = len(b.memory.messages)

    assert await b.loop._step_think() is False
    # Inactive cycle returns before recording — nothing committed.
    assert len(b.memory.messages) == before
