#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end progress-fidelity tests: engine → writer → msg_buffer.

These wire the *real* delivery chain — the frontier scheduler
(``_run_driver`` / ``_run_one_node``) reports via ``report_progress`` and a real
``make_progress_writer`` (configured with ``msg_buffer`` + ``wake``) renders
every push-worthy event straight into a ``MessageQueue``. No telemetry fan-out, no
subscriber, no stubs in between.

The contract under test (motivated by a production incident where the model
announced "image generation complete" while the image node was only ever
``running`` then ``cancelled``):

* A node that only reaches ``running`` MUST NOT produce any
  ``node_completed`` / ``success`` push.
* A graph whose nodes never all finish MUST NOT produce a ``task success``
  terminal push.
* A cancelled node pushes ``cancelled`` — never ``success``.
* Positive control: a graph that genuinely completes records a
  ``node_completed`` on disk (proving the harness captures real successes, so
  the negatives above are meaningful, not vacuous) — but does NOT push it. Node
  success is progress, not a decision, so it is disk-only; the whole-task
  ``task success`` terminal is delivered once by ``pool._on_done`` (also not by
  the writer). The writer's only mid-flight push is a node *failure*.
"""
from __future__ import annotations

import asyncio

from mote.contracts.conversation import MessagePriority, MessageQueue
from mote.orchestration.background_tasks.delivery import (
    make_progress_writer,
    reset_progress_writer,
    set_progress_writer,
)
from mote.orchestration.workflows import END, START, BgStatus, WorkflowBuilder
from mote.orchestration.workflows.engine import _run_driver, _run_one_node
from mote.orchestration.workflows.types import GraphRunState

from .conftest import S, gated_node, sync_node

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Harness:
    """deliver choke point + disk-backed progress writer (no bus, no subscriber).

    Mirrors the production wiring: the pool injects a single ``deliver`` callable
    (push + wake). Here ``deliver`` mimics it by pushing into a ``MessageQueue``
    at NEXT priority and recording a wake.
    """

    def __init__(self) -> None:
        self.msg_buffer = MessageQueue()
        self.wakes: list = []
        self.disk: list[str] = []

    def _deliver(self, notification) -> None:
        self.msg_buffer.push(notification, priority=MessagePriority.NEXT)
        self.wakes.append(1)

    def writer(self):
        """A progress writer that appends to disk and delivers to the buffer."""
        return make_progress_writer(
            self.disk.append,
            task_id="bg_1",
            deliver=self._deliver,
        )

    def buffer_contents(self) -> list[str]:
        """Drain the buffer and return the delivered message contents."""
        return [m.content for m in self.msg_buffer.pop_all()]


def _media_graph(node_fn) -> WorkflowBuilder:
    """A one-node ``media`` graph: START -> image -> END."""
    g = WorkflowBuilder("media", state_schema=S)
    g.add_node("image", node_fn)
    g.add_edge(START, "image")
    g.add_edge("image", END)
    return g


# ---------------------------------------------------------------------------
# Positive control — a real completion DOES surface success
# ---------------------------------------------------------------------------


def test_completed_graph_records_success_on_disk_without_pushing():
    h = _Harness()

    async def _run():
        g = _media_graph(sync_node(lambda s: "video.mp4", field="image"))
        state = g.state_schema(x=1)
        token = set_progress_writer(h.writer())
        try:
            await _run_driver(g, state, execute_nodes=["image"], initial_params={"x": 1})
        finally:
            reset_progress_writer(token)

    asyncio.run(_run())

    contents = h.buffer_contents()
    blob = "\n".join(contents)
    # The node really finished → its ``node_completed`` is recorded on disk...
    assert any("node_completed" in line for line in h.disk)
    assert any("[image] success" in line for line in h.disk)
    # ...but node success is disk-only (progress, not a decision), so NOTHING is
    # pushed here: neither the node completion nor the whole-task terminal (the
    # latter is delivered once by pool._on_done, not the writer).
    assert "node_completed" not in blob
    assert "task success" not in blob
    # The whole-task terminal's rich DAG snapshot still lands on disk.
    assert any("task success" in line for line in h.disk)


# ---------------------------------------------------------------------------
# The bug: running-only must never read as completed
# ---------------------------------------------------------------------------


def test_stuck_running_node_never_reports_completion():
    h = _Harness()

    async def _run():
        gate = asyncio.Event()  # never released while we snapshot
        g = _media_graph(gated_node(gate, lambda s: "video.mp4", field="image"))
        state = g.state_schema(x=1)
        token = set_progress_writer(h.writer())
        try:
            driver = asyncio.create_task(_run_driver(g, state, execute_nodes=["image"], initial_params={"x": 1}))
            # Wait until the node has reported RUNNING (it blocks on the gate
            # right after), then snapshot the buffer while it is stuck.
            for _ in range(200):
                await asyncio.sleep(0.01)
                if any("[image] running" in line for line in h.disk):
                    break
            contents = h.buffer_contents()
            # Release the gate so the driver finishes cleanly (no orphan task).
            gate.set()
            await driver
        finally:
            reset_progress_writer(token)
        return contents

    mid = asyncio.run(_run())

    blob = "\n".join(mid)
    # While only RUNNING, nothing is delivered at all (START is disk-only now,
    # node success/failure haven't happened) — and in particular NOTHING claims
    # the node or the task finished.
    assert "node_completed" not in blob
    assert "task success" not in blob
    assert "[image] success" not in blob
    # The node did report RUNNING on disk, but never SUCCESS at the time of snapshot.
    assert any("[image] running" in line for line in h.disk)


def test_cancelled_node_pushes_cancelled_not_success():
    h = _Harness()

    async def _run():
        gate = asyncio.Event()  # never released → node stays running until cancelled
        g = _media_graph(gated_node(gate, lambda s: "video.mp4", field="image"))
        state = g.state_schema(x=1)
        run_state = GraphRunState.for_graph(g)
        token = set_progress_writer(h.writer())
        try:
            node_task = asyncio.create_task(_run_one_node("image", state, g, set(), run_state))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if any("[image] running" in line for line in h.disk):
                    break
            node_task.cancel()
            try:
                await node_task
            except asyncio.CancelledError:
                pass
        finally:
            reset_progress_writer(token)
        return g, state, run_state

    g, state, run_state = asyncio.run(_run())

    contents = h.buffer_contents()
    blob = "\n".join(contents)
    # A cancelled node is disk-only now (not a decision point the writer pushes;
    # the whole-task cancel terminal is delivered once by pool._on_done). It is
    # recorded on disk as cancelled...
    assert any("cancelled" in line for line in h.disk)
    # ...and is never pushed at all, and never reported as a success/completion.
    assert blob == ""
    assert not any("[image] success" in line for line in h.disk)
    assert not any("node_completed" in line for line in h.disk)
    # Run-state status reflects the cancel; it never produced a result.
    assert run_state.get("image").status == BgStatus.CANCELLED
    assert getattr(state, "image", None) is None
