#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``InferenceService`` — the single think primitive.

Returns ``False`` immediately when the shared ``active`` signal is off (the
``End`` tool / ask_user "stop" path); otherwise it asks the context provider to
``prepare()`` the request, lazily ``resolve_llm()`` and hands everything to
``inference_engine.start()``, returning ``True``.
"""

from __future__ import annotations

import pytest

from mote.contracts.execution.models import InferenceCompleted, InferenceStopped

pytestmark = pytest.mark.asyncio


async def test_think_returns_false_when_inactive(make_engine):
    b = make_engine(active=False)
    b.engine._ctx = b.ctx

    assert isinstance(await b.engine._inference.infer(), InferenceStopped)
    # Nothing was prepared / started.
    assert b.provider.prepare_calls == 0
    assert b.inference_engine.start_calls == []


async def test_think_prepares_and_starts(make_engine):
    b = make_engine(active=True)
    b.engine._ctx = b.ctx

    assert isinstance(await b.engine._inference.infer(), InferenceCompleted)
    assert b.provider.prepare_calls == 1
    # The resolved route is driven off the full prepared InferenceRequest so route
    # selection can include model-call metadata as well as messages.
    assert b.provider.resolve_calls == [b.provider._think_request]

    call = b.inference_engine.start_calls[0]
    tr = b.provider._think_request
    assert call["req"] is tr.req
    assert call["system_prompt"] == tr.system_prompt
    assert call["tool_specs"] == tr.tool_specs
    assert call["target"] is b.provider.llm
    assert call["model_call_id"]
    assert call["resume"] is False


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


async def test_think_records_non_empty_turn_context_to_memory(make_engine):
    bus = _FakeTurnContextBus("<system-reminder>\ngit changed\n</system-reminder>")
    b = make_engine(active=True, turn_context_bus=bus)
    b.engine._ctx = b.ctx

    await b.engine._inference.infer()

    # The persisted block landed in history as a user message.
    added = [m for m in b.memory.messages if m.content == "<system-reminder>\ngit changed\n</system-reminder>"]
    assert len(added) == 1
    assert added[0].role == "user"


async def test_think_empty_turn_context_adds_nothing(make_engine):
    bus = _FakeTurnContextBus("")
    b = make_engine(active=True, turn_context_bus=bus)
    b.engine._ctx = b.ctx
    before = len(b.memory.messages)

    await b.engine._inference.infer()

    assert len(b.memory.messages) == before


async def test_think_passes_live_cwd_to_turn_context_bus(make_engine):
    bus = _FakeTurnContextBus("")
    b = make_engine(active=True, turn_context_bus=bus, get_cwd=lambda: "/some/dir")
    b.engine._ctx = b.ctx

    await b.engine._inference.infer()

    assert bus.seen_cwd == "/some/dir"


async def test_think_no_turn_context_bus_records_nothing(make_engine):
    # turn_context_bus defaults to None — recording is a no-op, think still runs.
    b = make_engine(active=True)
    b.engine._ctx = b.ctx
    before = len(b.memory.messages)

    assert isinstance(await b.engine._inference.infer(), InferenceCompleted)
    assert len(b.memory.messages) == before


async def test_think_records_turn_context_before_prepare(make_engine):
    """The block is in history by the time prepare() builds the request, so the
    turn-context is visible to this cycle's think (order: commit -> prepare)."""
    bus = _FakeTurnContextBus("<system-reminder>\nstate\n</system-reminder>")
    b = make_engine(active=True, turn_context_bus=bus)
    b.engine._ctx = b.ctx

    seen_at_prepare: list[int] = []
    orig_prepare = b.provider.prepare

    async def spy_prepare():
        seen_at_prepare.append(len(b.memory.messages))
        return await orig_prepare()

    b.provider.prepare = spy_prepare

    await b.engine._inference.infer()

    # At least one message (the recorded block) was already committed when
    # prepare() ran.
    assert seen_at_prepare and seen_at_prepare[0] >= 1


async def test_think_inactive_skips_turn_context_recording(make_engine):
    bus = _FakeTurnContextBus("<system-reminder>\nx\n</system-reminder>")
    b = make_engine(active=False, turn_context_bus=bus)
    b.engine._ctx = b.ctx
    before = len(b.memory.messages)

    assert isinstance(await b.engine._inference.infer(), InferenceStopped)
    # Inactive cycle returns before recording — nothing committed.
    assert len(b.memory.messages) == before
