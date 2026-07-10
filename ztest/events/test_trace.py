#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the framework-native ``span`` trace primitive.

``span`` emits a SpanStart/SpanEnd pair on the active bus carrying explicit
``span_id`` / ``parent_span_id`` / ``trace_id``, sets the current-span contextvar
for the duration of the body, and marks an error status when the body raises
(re-raising). With no bus bound it stays a near-no-op (mints a uuid, runs the
body, emits nothing).
"""
from __future__ import annotations

import asyncio

from metagpt.common.events import (
    EventBus,
    SpanEndEvent,
    SpanStartEvent,
    current_span_id,
    set_bus,
    span,
)
from metagpt.common.interface.event_subscriber import ObservationSubscriber
from metagpt.common.logs import bind_trace


def run(coro):
    return asyncio.run(coro)


class _Capture(ObservationSubscriber):
    priority = 50

    def __init__(self):
        self.events: list = []

    async def handle(self, event):
        self.events.append(event)
        return None


def _bus():
    bus = EventBus()
    cap = _Capture()
    bus.subscribe(cap)
    return bus, cap


def test_span_emits_start_then_end_with_matching_id():
    bus, cap = _bus()

    async def go():
        with set_bus(bus):
            async with span("think") as sid:
                assert current_span_id() == sid

    run(go())

    starts = [e for e in cap.events if isinstance(e, SpanStartEvent)]
    ends = [e for e in cap.events if isinstance(e, SpanEndEvent)]
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0].label == "think"
    assert starts[0].parent_span_id is None
    assert starts[0].span_id == ends[0].span_id
    assert ends[0].status == "ok"


def test_nested_span_carries_parent_span_id():
    bus, cap = _bus()

    async def go():
        with set_bus(bus):
            async with span("outer") as outer_id:
                async with span("inner") as inner_id:
                    return outer_id, inner_id

    outer_id, inner_id = run(go())

    starts = {e.label: e for e in cap.events if isinstance(e, SpanStartEvent)}
    assert starts["outer"].span_id == outer_id
    assert starts["inner"].span_id == inner_id
    assert starts["outer"].parent_span_id is None
    assert starts["inner"].parent_span_id == outer_id


def test_trace_id_taken_from_bind_trace():
    bus, cap = _bus()

    async def go():
        with bind_trace("sess-123"), set_bus(bus):
            async with span("root"):
                pass

    run(go())

    start = next(e for e in cap.events if isinstance(e, SpanStartEvent))
    end = next(e for e in cap.events if isinstance(e, SpanEndEvent))
    assert start.trace_id == "sess-123"
    assert end.trace_id == "sess-123"


def test_exception_marks_error_and_reraises():
    bus, cap = _bus()

    async def go():
        with set_bus(bus):
            async with span("boom"):
                raise ValueError("kaboom")

    raised = False
    try:
        run(go())
    except ValueError:
        raised = True
    assert raised

    end = next(e for e in cap.events if isinstance(e, SpanEndEvent))
    assert end.status == "error"
    assert "ValueError" in end.error and "kaboom" in end.error


def test_current_span_id_restored_on_exit():
    bus, _ = _bus()

    async def go():
        with set_bus(bus):
            assert current_span_id() is None
            async with span("s"):
                assert current_span_id() is not None
            assert current_span_id() is None

    run(go())


def test_no_bus_emits_nothing_but_body_runs():
    ran = {"v": False}

    async def go():
        async with span("orphan") as sid:
            ran["v"] = True
            assert sid  # a uuid is still minted
            assert current_span_id() == sid

    run(go())
    assert ran["v"] is True
