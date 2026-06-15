#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for LogSubscriber: it logs semantic events, never folds, never raises."""
from __future__ import annotations

import pytest

from metagpt.common.events import (
    EventBus,
    LLMStreamDeltaEvent,
    LogSubscriber,
    PreToolUseEvent,
    SessionStartEvent,
    emit_event,
    emit_event_sync,
    set_bus,
)
from metagpt.common.events.log_subscriber import _clip


def _capture(monkeypatch):
    """Patch the subscriber's logger; return (info_lines, debug_lines, warns)."""
    info, debug, warns = [], [], []
    import metagpt.common.events.log_subscriber as mod

    monkeypatch.setattr(mod.logger, "info", lambda m: info.append(m))
    monkeypatch.setattr(mod.logger, "debug", lambda m: debug.append(m))
    monkeypatch.setattr(mod.logger, "warning", lambda m: warns.append(m))
    return info, debug, warns


@pytest.mark.asyncio
async def test_session_start_logged_at_info(monkeypatch):
    info, debug, _ = _capture(monkeypatch)
    sub = LogSubscriber()
    out = await sub.handle(SessionStartEvent(session_id="abcd1234ef", source="startup", model="gpt"))
    assert out is None  # never folds an outcome
    assert len(info) == 1 and "session_start" in info[0] and "abcd1234" in info[0]
    assert debug == []


@pytest.mark.asyncio
async def test_tool_event_logged_at_debug(monkeypatch):
    info, debug, _ = _capture(monkeypatch)
    await LogSubscriber().handle(PreToolUseEvent(tool_name="Read", tool_input={"x": 1}))
    assert info == []
    assert len(debug) == 1 and "pre_tool_use" in debug[0] and "Read" in debug[0]


@pytest.mark.asyncio
async def test_handle_swallows_errors(monkeypatch):
    info, debug, warns = _capture(monkeypatch)

    class _Boom:
        name = "boom"

        def __getattr__(self, _):  # any field access explodes
            raise RuntimeError("nope")

    # An unknown event type hits the else-branch which only reads .name — make
    # even that raise to exercise the guard.
    bad = _Boom()
    out = await LogSubscriber().handle(bad)
    assert out is None  # best-effort: never raises into the bus


@pytest.mark.asyncio
async def test_stream_deltas_are_not_logged(monkeypatch):
    # Deltas are delivered via emit_sync -> handle_sync, which LogSubscriber does
    # not implement, so per-token chunks never reach the logger.
    info, debug, _ = _capture(monkeypatch)
    bus = EventBus()
    bus.subscribe(LogSubscriber())
    with set_bus(bus):
        emit_event_sync(LLMStreamDeltaEvent(token="tok"))
    assert info == [] and debug == []


@pytest.mark.asyncio
async def test_subscribed_on_bus_logs_via_emit(monkeypatch):
    info, debug, _ = _capture(monkeypatch)
    bus = EventBus()
    bus.subscribe(LogSubscriber())
    with set_bus(bus):
        await emit_event(SessionStartEvent(session_id="zzzz", source="resume"))
    assert any("session_start" in line for line in info)


def test_clip_truncates_and_collapses_whitespace():
    assert _clip("a  b\n c") == "a b c"
    long = "x" * 200
    out = _clip(long, limit=10)
    assert len(out) == 10 and out.endswith("…")
