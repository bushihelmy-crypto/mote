#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the bggraph progress writer's dual sink: disk append + bus emit.

``make_progress_writer`` keeps the on-disk append as the source of truth and,
when a ``task_id`` is supplied, also mirrors each event onto the active bus as a
:class:`TaskProgressEvent` (so subscribers see live progress without polling).
"""
from __future__ import annotations

import pytest
from mote.common.events import EventBus, TaskProgressEvent, set_bus
from mote.common.interface.event_subscriber import ObservationSubscriber, SyncObserver
from mote.executor.tasks.bggraph.report import MAX_RESULT_DISPLAY_CHARS, make_progress_writer


class _Recorder(ObservationSubscriber, SyncObserver):
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


def test_detail_is_not_truncated_in_either_sink():
    # Truncation was deliberately removed — full detail flows through both sinks
    # so notifications/progress are never cut mid-content.
    lines = []
    bus, rec = _bus()
    writer = make_progress_writer(lines.append, task_id="bg_3")
    big = "y" * (MAX_RESULT_DISPLAY_CHARS + 100)
    with set_bus(bus):
        writer("node", "running", big)
    assert big in lines[0]
    assert rec.events[0].detail == big


def test_no_bus_still_appends():
    lines = []
    writer = make_progress_writer(lines.append, task_id="bg_4")
    # No set_bus -> emit is a no-op; the append must still happen.
    writer("split", "running", "x")
    assert lines == ["[split] running: x\n"]


# ---------------------------------------------------------------------------
# Structured delivery via the injected ``deliver`` choke point (no event bus)
# ---------------------------------------------------------------------------


def test_push_worthy_status_delivers_structured_notification():
    from mote.executor.tasks.types import BackgroundTaskNotification

    lines, delivered = [], []
    writer = make_progress_writer(lines.append, task_id="bg_1", command_name="my_graph", deliver=delivered.append)
    writer("split", "success", "node done")
    # Terminal status is push-worthy → one structured notification.
    assert len(delivered) == 1
    n = delivered[0]
    assert isinstance(n, BackgroundTaskNotification)
    assert (n.content, n.task_id, n.command_name, n.status) == (
        "node done",
        "bg_1",
        "my_graph",
        "success",
    )
    # Still appended to disk.
    assert lines == ["[split] success: node done\n"]


def test_running_status_not_delivered():
    lines, delivered = [], []
    writer = make_progress_writer(lines.append, task_id="bg_1", deliver=delivered.append)
    writer("split", "running", "still going")
    # A mid-flight running update lands only on disk — not push-worthy.
    assert delivered == []
    assert lines == ["[split] running: still going\n"]


def test_graph_start_marker_is_delivered():
    from mote.executor.tasks.bggraph.types import END

    delivered = []
    writer = make_progress_writer(lambda _l: None, task_id="bg_1", deliver=delivered.append)
    # The graph-level START marker (END stage + running) is a push-worthy heads-up.
    writer(END, "running", "task started")
    assert [m.content for m in delivered] == ["task started"]
    # ...but it is NOT the whole-task terminal.
    assert delivered[0].task_terminal is False


def test_writer_delivers_only_non_terminal_events():
    from mote.executor.tasks.bggraph.types import END

    delivered = []
    writer = make_progress_writer(lambda _l: None, task_id="bg_1", deliver=delivered.append)
    # Per-node terminal → mid-flight, delivered (pushes a new react turn).
    writer("split", "success", "node done")
    # Graph START heads-up → delivered.
    writer(END, "running", "started")
    # Graph-level terminal → NOT delivered by the writer; pool._on_done is the
    # sole producer of the one whole-task terminal.
    writer(END, "success", "graph done")
    # Route pause → also a whole-task outcome → NOT delivered here.
    writer("router_node", "waiting_for_route", "pick a route")

    # Only the two non-terminal events reach deliver, and neither is flagged
    # task_terminal (the writer never emits a terminal anymore).
    assert [(m.status, m.task_terminal) for m in delivered] == [
        ("success", False),  # node terminal (mid-flight)
        ("running", False),  # START
    ]


def test_current_placeholder_substituted_with_task_id():
    lines, delivered = [], []
    bus, rec = _bus()
    writer = make_progress_writer(lines.append, task_id="bg_7", deliver=delivered.append)
    with set_bus(bus):
        writer("split", "success", "task (current) finished")
    # Substitution now happens in the writer, so every sink sees the real id:
    # disk append, event bus and the delivered notification — never the literal.
    assert lines == ["[split] success: task bg_7 finished\n"]
    assert rec.events[0].detail == "task bg_7 finished"
    assert [m.content for m in delivered] == ["task bg_7 finished"]


def test_current_placeholder_kept_without_task_id():
    # No task_id → nothing to substitute with; the literal is preserved.
    lines = []
    writer = make_progress_writer(lines.append)  # task_id="" default
    writer("split", "success", "task (current) finished")
    assert lines == ["[split] success: task (current) finished\n"]
