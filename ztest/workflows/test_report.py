#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the bggraph progress writer's disk and telemetry sinks.

``make_progress_writer`` keeps the on-disk append as the source of truth and,
when a ``task_id`` is supplied, also mirrors each event onto active telemetry.
"""
from __future__ import annotations

import pytest

from mote.orchestration.background_tasks.delivery import make_progress_writer
from mote.runtime.events import TaskProgressEvent, bind_telemetry
from mote.ztest.telemetry import InlineTelemetry


class _Recorder:
    def __init__(self):
        self.events = []

    def handle_sync(self, event):
        if isinstance(event, TaskProgressEvent):
            self.events.append(event)

    async def handle(self, event):
        return None


def _telemetry():
    rec = _Recorder()
    return InlineTelemetry(rec), rec


def test_writer_appends_and_emits():
    lines = []
    telemetry, rec = _telemetry()
    writer = make_progress_writer(lines.append, task_id="bg_1")
    with bind_telemetry(telemetry):
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
    telemetry, rec = _telemetry()
    writer = make_progress_writer(lines.append, task_id="bg_2")
    with bind_telemetry(telemetry):
        writer("merge", _Status(), None)
    assert lines == ["[merge] DONE: \n"]
    assert rec.events[0].status == "DONE" and rec.events[0].detail == ""


def test_empty_task_id_appends_but_does_not_emit():
    lines = []
    telemetry, rec = _telemetry()
    writer = make_progress_writer(lines.append)  # task_id="" default
    with bind_telemetry(telemetry):
        writer("split", "running", "x")
    assert lines == ["[split] running: x\n"]
    assert rec.events == []


def test_detail_is_not_truncated_in_either_sink():
    # Truncation was deliberately removed — full detail flows through both sinks
    # so notifications/progress are never cut mid-content.
    lines = []
    telemetry, rec = _telemetry()
    writer = make_progress_writer(lines.append, task_id="bg_3")
    big = "y" * 200_000
    with bind_telemetry(telemetry):
        writer("node", "running", big)
    assert big in lines[0]
    assert rec.events[0].detail == big


def test_no_telemetry_still_appends():
    lines = []
    writer = make_progress_writer(lines.append, task_id="bg_4")
    # No telemetry -> emit is a no-op; the append must still happen.
    writer("split", "running", "x")
    assert lines == ["[split] running: x\n"]


# ---------------------------------------------------------------------------
# Structured delivery via the injected ``deliver`` choke point
# ---------------------------------------------------------------------------


def test_node_failure_delivers_structured_notification():
    from mote.orchestration.background_tasks.model import BackgroundTaskNotification

    lines, delivered = [], []
    writer = make_progress_writer(lines.append, task_id="bg_1", command_name="my_graph", deliver=delivered.append)
    writer("split", "failed", "node blew up")
    # A per-node failure is the sole mid-flight decision point → one push.
    assert len(delivered) == 1
    n = delivered[0]
    assert isinstance(n, BackgroundTaskNotification)
    assert (n.content, n.task_id, n.command_name, n.status) == (
        "node blew up",
        "bg_1",
        "my_graph",
        "failed",
    )
    # A mid-flight failure is NOT the whole-task terminal (pool._on_done owns that).
    assert n.task_terminal is False
    # Still appended to disk.
    assert lines == ["[split] failed: node blew up\n"]


def test_node_success_not_delivered():
    lines, delivered = [], []
    writer = make_progress_writer(lines.append, task_id="bg_1", deliver=delivered.append)
    writer("split", "success", "node done")
    # node_completed is progress, not a decision → disk-only, no push. The final
    # result reaches the agent once via the whole-task terminal (pool._on_done).
    assert delivered == []
    assert lines == ["[split] success: node done\n"]


def test_running_status_not_delivered():
    lines, delivered = [], []
    writer = make_progress_writer(lines.append, task_id="bg_1", deliver=delivered.append)
    writer("split", "running", "still going")
    # A mid-flight running update lands only on disk — not push-worthy.
    assert delivered == []
    assert lines == ["[split] running: still going\n"]


def test_graph_start_marker_not_delivered():
    from mote.orchestration.workflows.types import END

    lines, delivered = [], []
    writer = make_progress_writer(lines.append, task_id="bg_1", deliver=delivered.append)
    # The graph-level START marker (END stage + running) is disk-only now — the
    # tool's own return value already carries the stage-summary at submit time.
    writer(END, "running", "task started")
    assert delivered == []
    assert lines == ["[__end__] running: task started\n"]


def test_writer_delivers_only_node_failures():
    from mote.orchestration.workflows.types import END

    delivered = []
    writer = make_progress_writer(lambda _l: None, task_id="bg_1", deliver=delivered.append)
    # Node success → disk-only (progress, not a decision).
    writer("split", "success", "node done")
    # Graph START heads-up → disk-only (stage-summary already returned at submit).
    writer(END, "running", "started")
    # Graph-level terminal → pool._on_done is the sole producer of that terminal.
    writer(END, "success", "graph done")
    # Route pause → whole-task outcome → pool._on_done, not the writer.
    writer("router_node", "waiting_for_route", "pick a route")
    # Node failure → the ONE mid-flight decision point the writer pushes.
    writer("tts", "failed", "boom")

    # Only the node failure reaches deliver, and it is not flagged task_terminal.
    assert [(m.status, m.task_terminal) for m in delivered] == [
        ("failed", False),
    ]


def test_current_placeholder_substituted_with_task_id():
    lines, delivered = [], []
    telemetry, rec = _telemetry()
    writer = make_progress_writer(lines.append, task_id="bg_7", deliver=delivered.append)
    with bind_telemetry(telemetry):
        # A node failure is the one event that reaches all three sinks (disk +
        # bus + deliver), so it exercises substitution on the delivered path too.
        writer("split", "failed", "task (current) finished")
    # Substitution now happens in the writer, so every sink sees the real id:
    # disk append, telemetry observation, and delivered notification — never the literal.
    assert lines == ["[split] failed: task bg_7 finished\n"]
    assert rec.events[0].detail == "task bg_7 finished"
    assert [m.content for m in delivered] == ["task bg_7 finished"]


def test_current_placeholder_kept_without_task_id():
    # No task_id → nothing to substitute with; the literal is preserved.
    lines = []
    writer = make_progress_writer(lines.append)  # task_id="" default
    writer("split", "success", "task (current) finished")
    assert lines == ["[split] success: task (current) finished\n"]
