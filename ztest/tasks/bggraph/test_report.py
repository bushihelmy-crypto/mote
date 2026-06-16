#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the bggraph progress writer's dual sink: disk append + bus emit.

``make_progress_writer`` keeps the on-disk append as the source of truth and,
when a ``task_id`` is supplied, also mirrors each event onto the active bus as a
:class:`TaskProgressEvent` (so subscribers see live progress without polling).
"""
from __future__ import annotations

import pytest

from metagpt.common.events import EventBus, TaskProgressEvent, set_bus
from metagpt.executor.tasks.bggraph.report import MAX_RESULT_DISPLAY_CHARS, make_progress_writer


class _Recorder:
    priority = 50

    def __init__(self):
        self.events = []

    def handle_sync(self, event):
        if isinstance(event, TaskProgressEvent):
            self.events.append(event)

    async def handle(self, event):
        return None


def _bus():
    bus = EventBus()
    rec = _Recorder()
    bus.subscribe(rec)
    return bus, rec


def test_writer_appends_and_emits():
    lines = []
    bus, rec = _bus()
    writer = make_progress_writer(lines.append, task_id="bg_1")
    with set_bus(bus):
        writer("split", "running", "detail-text")
    # disk append (source of truth)
    assert lines == ["[split] running: detail-text\n"]
    # bus mirror
    assert len(rec.events) == 1
    e = rec.events[0]
    assert (e.task_id, e.stage, e.status, e.detail) == ("bg_1", "split", "running", "detail-text")


def test_status_enum_value_used():
    class _Status:
        value = "DONE"

    lines = []
    bus, rec = _bus()
    writer = make_progress_writer(lines.append, task_id="bg_2")
    with set_bus(bus):
        writer("merge", _Status(), None)
    assert lines == ["[merge] DONE: \n"]
    assert rec.events[0].status == "DONE" and rec.events[0].detail == ""


def test_empty_task_id_appends_but_does_not_emit():
    lines = []
    bus, rec = _bus()
    writer = make_progress_writer(lines.append)  # task_id="" default
    with set_bus(bus):
        writer("split", "running", "x")
    assert lines == ["[split] running: x\n"]
    assert rec.events == []


def test_detail_is_truncated_in_both_sinks():
    lines = []
    bus, rec = _bus()
    writer = make_progress_writer(lines.append, task_id="bg_3")
    big = "y" * (MAX_RESULT_DISPLAY_CHARS + 100)
    with set_bus(bus):
        writer("node", "running", big)
    assert "TRUNCATED" in lines[0]
    assert "TRUNCATED" in rec.events[0].detail
    assert len(rec.events[0].detail) < len(big)


def test_no_bus_still_appends():
    lines = []
    writer = make_progress_writer(lines.append, task_id="bg_4")
    # No set_bus -> emit is a no-op; the append must still happen.
    writer("split", "running", "x")
    assert lines == ["[split] running: x\n"]
