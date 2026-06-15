#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the EventBus spine: ordering, fold, error isolation, sync emit."""
from __future__ import annotations

from typing import Optional

import pytest

from metagpt.common.events import (
    EventBus,
    LLMStreamDeltaEvent,
    PreToolUseEvent,
    current_bus,
    emit_event,
    emit_event_sync,
    set_bus,
)
from metagpt.common.hook.types import HookOutcome


class RecordingSub:
    """A subscriber that records the events it sees and returns a fixed outcome."""

    def __init__(self, priority: int, log: list, *, outcome: Optional[HookOutcome] = None, tag: str = ""):
        self.priority = priority
        self._log = log
        self._outcome = outcome
        self._tag = tag

    async def handle(self, event) -> Optional[HookOutcome]:
        self._log.append((self._tag or self.priority, event.name))
        return self._outcome


class SyncSub:
    def __init__(self, priority: int, log: list):
        self.priority = priority
        self._log = log

    async def handle(self, event):
        return None

    def handle_sync(self, event):
        self._log.append(("sync", event.name))


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_subscribe_orders_by_ascending_priority():
    bus = EventBus()
    log: list = []
    bus.subscribe(RecordingSub(50, log, tag="mid"))
    bus.subscribe(RecordingSub(10, log, tag="low"))
    bus.subscribe(RecordingSub(80, log, tag="high"))
    tags = [getattr(s, "_tag") for s in bus.subscribers]
    assert tags == ["low", "mid", "high"]


def test_equal_priority_keeps_insertion_order():
    bus = EventBus()
    log: list = []
    bus.subscribe(RecordingSub(10, log, tag="first"))
    bus.subscribe(RecordingSub(10, log, tag="second"))
    tags = [getattr(s, "_tag") for s in bus.subscribers]
    assert tags == ["first", "second"]


@pytest.mark.asyncio
async def test_emit_dispatches_in_priority_order():
    bus = EventBus()
    log: list = []
    bus.subscribe(RecordingSub(50, log, tag="mid"))
    bus.subscribe(RecordingSub(10, log, tag="low"))
    await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert log == [("low", "llm_stream_delta"), ("mid", "llm_stream_delta")]


# ---------------------------------------------------------------------------
# Fold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_folds_deny_over_allow():
    bus = EventBus()
    log: list = []
    bus.subscribe(RecordingSub(10, log, outcome=HookOutcome(behavior="allow")))
    bus.subscribe(RecordingSub(20, log, outcome=HookOutcome(behavior="deny")))
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert out.behavior == "deny"


@pytest.mark.asyncio
async def test_emit_accumulates_additional_context():
    bus = EventBus()
    log: list = []
    bus.subscribe(RecordingSub(10, log, outcome=HookOutcome(additional_context=["a"])))
    bus.subscribe(RecordingSub(20, log, outcome=HookOutcome(additional_context=["b"])))
    out = await bus.emit(PreToolUseEvent(tool_name="Bash"))
    assert out.additional_context == ["a", "b"]


@pytest.mark.asyncio
async def test_emit_with_no_outcomes_returns_empty():
    bus = EventBus()
    log: list = []
    bus.subscribe(RecordingSub(10, log))  # returns None
    out = await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert out.behavior is None
    assert not out.additional_context


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_subscriber_does_not_break_stream():
    bus = EventBus()
    log: list = []

    class Boom:
        priority = 5

        async def handle(self, event):
            raise RuntimeError("boom")

    bus.subscribe(Boom())
    bus.subscribe(RecordingSub(10, log, tag="ok"))
    out = await bus.emit(LLMStreamDeltaEvent(token="x"))
    assert log == [("ok", "llm_stream_delta")]
    assert out.behavior is None


# ---------------------------------------------------------------------------
# Sync emit
# ---------------------------------------------------------------------------


def test_emit_sync_only_reaches_sync_subscribers():
    bus = EventBus()
    log: list = []
    bus.subscribe(RecordingSub(10, log, tag="async-only"))  # no handle_sync
    bus.subscribe(SyncSub(20, log))
    bus.emit_sync(LLMStreamDeltaEvent(token="x"))
    assert log == [("sync", "llm_stream_delta")]


def test_emit_sync_isolates_errors():
    bus = EventBus()

    class BoomSync:
        priority = 5

        async def handle(self, event):
            return None

        def handle_sync(self, event):
            raise RuntimeError("boom")

    log: list = []
    bus.subscribe(BoomSync())
    bus.subscribe(SyncSub(10, log))
    bus.emit_sync(LLMStreamDeltaEvent(token="x"))  # must not raise
    assert log == [("sync", "llm_stream_delta")]


# ---------------------------------------------------------------------------
# Contextvar binding
# ---------------------------------------------------------------------------


def test_current_bus_unbound_is_none():
    assert current_bus() is None


@pytest.mark.asyncio
async def test_emit_event_no_op_without_bus():
    out = await emit_event(LLMStreamDeltaEvent(token="x"))  # no bus bound
    assert out.behavior is None


@pytest.mark.asyncio
async def test_emit_event_routes_to_bound_bus():
    bus = EventBus()
    log: list = []
    bus.subscribe(RecordingSub(10, log, tag="bound"))
    with set_bus(bus):
        assert current_bus() is bus
        await emit_event(LLMStreamDeltaEvent(token="tok"))
    assert current_bus() is None  # reset on exit
    assert log == [("bound", "llm_stream_delta")]


def test_emit_event_sync_no_op_without_bus():
    emit_event_sync(LLMStreamDeltaEvent(token="x"))  # must not raise


def test_emit_event_sync_routes_to_bound_bus():
    bus = EventBus()
    log: list = []
    bus.subscribe(SyncSub(10, log))
    with set_bus(bus):
        emit_event_sync(LLMStreamDeltaEvent(token="tok"))
    assert log == [("sync", "llm_stream_delta")]
