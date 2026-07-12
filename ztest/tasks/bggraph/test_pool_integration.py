#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end integration: ``BgGraph`` compiled task driven by the real pool.

Wires a compiled graph's ``poll`` coroutine through ``BackgroundTaskPool`` with
a ``TaskOutputStore`` progress sink, then asserts:

* notifications (START / per-node / END) land in the msg_buffer (the pool /
  progress writer push directly — no event bus), and
* per-node ``report_progress`` events were appended to the task's disk output
  (the basis for ``<delta-summary>`` blocks).
"""
from __future__ import annotations

import asyncio

import pytest

from metagpt.common.schema import MessageQueue
from metagpt.executor.tasks import BackgroundTaskPool, TaskOutputStore, BackgroundTaskNotification, BgStatus
from metagpt.executor.tasks.bggraph import END, START, BgGraph
from metagpt.executor.tasks.bggraph.types import LlmPauseResult

from .conftest import S, sync_node, gated_node

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path):
    return TaskOutputStore(base_dir=tmp_path)


@pytest.fixture
def msg_buffer():
    return MessageQueue()


@pytest.fixture
def pool(msg_buffer, store):
    return BackgroundTaskPool(msg_buffer=msg_buffer, output_store=store)


def _media_graph() -> BgGraph:
    g = BgGraph("media", state_schema=S)
    g.add_node("split", sync_node(lambda s: {"parts": 2}, field="split"))
    g.add_node("tts", sync_node(lambda s: "audio", field="tts"))
    g.add_node("render", sync_node(lambda s: "video", field="render"))
    g.add_node("merge", sync_node(lambda s: {"out": [s.tts, s.render]}, field="merge"))
    g.add_edge(START, "split")
    g.add_edge("split", "tts")
    g.add_edge("split", "render")
    g.add_edge(["tts", "render"], "merge")
    g.add_edge("merge", END)
    return g


async def _drain_msgs(msg_buffer) -> list:
    msgs = []
    while True:
        m = msg_buffer.pop()
        if m is None:
            break
        msgs.append(m)
    return msgs


async def _flush(store, tid) -> str:
    """Force the async disk-drain loop to flush, then return the output text.

    ``DiskTaskOutput.append`` is queued and written by a background drain loop,
    so a read right after ``wait_all`` can race ahead of the final writes.
    Closing the per-task output drains the queue deterministically; the entry
    stays registered so ``get_tail`` can still read it.
    """
    await store._outputs[tid].close()
    return (await store.get_tail(tid)).decode("utf-8", errors="replace")


class TestPoolIntegration:
    async def test_success_notifies_and_records_progress(self, pool, store, msg_buffer):
        g = _media_graph()
        res = await g.compile()(x=0)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=None, progress=True)
        await pool.wait_all()

        # Graph tasks push via the progress writer → UserMessage in the buffer.
        # Expect: START + per-node completions + terminal END.
        msgs = await _drain_msgs(msg_buffer)
        assert len(msgs) >= 2  # at minimum START + END
        # Terminal notification contains "success".
        contents = " ".join(m.content for m in msgs)
        assert "success" in contents
        assert tid in contents  # real task_id injected

        # Meta is terminal/success.
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.SUCCESS

        # Per-node progress + terminal summary were appended to disk output.
        tail = await _flush(store, tid)
        assert "split" in tail
        assert "tts" in tail
        assert "render" in tail
        assert "merge" in tail
        assert "success" in tail

    async def test_failure_notifies_failed(self, pool, store, msg_buffer):
        g = BgGraph("boomgraph", state_schema=S)

        def _raise(_s):
            raise ValueError("kaboom")

        g.add_node("a", sync_node(_raise))
        g.add_edge(START, "a")
        g.add_edge("a", END)

        res = await g.compile()(x=0)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=None, progress=True)
        await pool.wait_all()

        msgs = await _drain_msgs(msg_buffer)
        contents = " ".join(m.content for m in msgs)
        assert "failed" in contents or "kaboom" in contents

        # Disk output captured the node-failure + terminal-failed summary.
        tail = await _flush(store, tid)
        assert "kaboom" in tail

    async def test_progress_disabled_no_store_write(self, pool, store, msg_buffer):
        """Without ``progress=True`` the graph still runs, but no disk sink is set.
        Without a progress writer, the completion is pushed directly by _on_done."""
        g = _media_graph()
        res = await g.compile()(x=0)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=None)
        await pool.wait_all()

        msgs = await _drain_msgs(msg_buffer)
        notes = [m for m in msgs if isinstance(m, BackgroundTaskNotification)]
        assert len(notes) == 1
        assert notes[0].status == BgStatus.SUCCESS
        # No output was initialised for this task.
        with pytest.raises(KeyError):
            await store.get_tail(tid)


def _llm_pause_graph() -> BgGraph:
    """Graph that pauses on an LLM edge after node 'a'."""
    g = BgGraph("llmpause", state_schema=S)
    g.add_node("a", sync_node(lambda s: "a-done", field="a"))
    g.add_node("nextstep", sync_node(lambda s: "next-done", field="nextstep"))
    g.add_edge(START, "a")
    g.add_llm_edges("a", "Pick route", {"go": "nextstep", "stop": END})
    g.add_edge("nextstep", END)
    return g


class TestPoolPauseAndResubmit:
    """Pool correctly identifies LlmPauseResult and supports resubmit."""

    async def test_pause_sets_waiting_for_route(self, pool, store, msg_buffer):
        """When graph pauses on an LLM edge, pool marks WAITING_FOR_ROUTE."""
        g = _llm_pause_graph()
        res = await g.compile()(x=0)
        tid = pool.submit(
            res.poll_factory,
            res.command_name,
            timeout=None,
            progress=True,
            graph_meta=res.graph_meta,
        )
        await pool.wait_all()

        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.WAITING_FOR_ROUTE
        assert meta.state_snapshot is not None
        assert "a" in meta.completed_nodes
        assert meta.graph_meta is not None
        assert meta.graph_meta.graph_ref is not None

        # Notifications pushed via the progress writer (START + node + llm_route).
        msgs = await _drain_msgs(msg_buffer)
        assert len(msgs) >= 1
        contents = " ".join(m.content for m in msgs)
        assert "waiting_for_route" in contents or "route" in contents.lower()

    async def test_resubmit_resumes_to_success(self, pool, store, msg_buffer):
        """resubmit() with a fresh coro runs to completion under same task_id."""
        g = _llm_pause_graph()
        res = await g.compile()(x=0)
        tid = pool.submit(
            res.poll_factory,
            res.command_name,
            timeout=None,
            progress=True,
            graph_meta=res.graph_meta,
        )
        await pool.wait_all()

        # Drain pause notification.
        await _drain_msgs(msg_buffer)

        # Simulate resume: run a simple coro that returns a value.
        async def resumed():
            return "resumed-result"

        returned_id = pool.resubmit(tid, lambda: resumed(), progress=False)
        assert returned_id == tid
        await pool.wait_all()

        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.SUCCESS
        assert meta.retry_count == 1

        # Completion notification pushed (no progress → direct _on_done push).
        msgs = await _drain_msgs(msg_buffer)
        notes = [m for m in msgs if isinstance(m, BackgroundTaskNotification)]
        assert len(notes) == 1
        assert notes[0].status == BgStatus.SUCCESS

    async def test_resubmit_unknown_id_raises(self, pool, store, msg_buffer):
        """resubmit() with an unknown task_id raises ValueError."""
        async def noop():
            return None

        with pytest.raises(ValueError, match="Unknown task_id"):
            pool.resubmit("bg_999", lambda: noop())


def _timeout_graph(gate: "asyncio.Event") -> BgGraph:
    """Graph whose first node completes then second node hangs on *gate*.

    Drives a deterministic timeout: ``first`` returns immediately (recorded
    SUCCESS), ``second`` blocks until the gate is set — which the test never
    does, so the per-task timeout fires while ``second`` is still running.
    """
    g = BgGraph("hanggraph", state_schema=S)
    g.add_node("first", sync_node(lambda s: "first-done", field="first"))
    g.add_node("second", gated_node(gate, lambda s: "second-done", field="second"))
    g.add_edge(START, "first")
    g.add_edge("first", "second")
    g.add_edge("second", END)
    return g


class TestPoolTimeoutSnapshot:
    """Timeout must snapshot graph state so resume continues, not restarts."""

    async def test_timeout_captures_state_and_run_state(self, pool, store, msg_buffer):
        gate = asyncio.Event()  # never set → second node hangs
        g = _timeout_graph(gate)
        res = await g.compile()(x=0)
        tid = pool.submit(
            res.poll_factory,
            res.command_name,
            timeout=0.1,
            progress=True,
            graph_meta=res.graph_meta,
        )
        await pool.wait_all()

        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.TIMEOUT
        # The fix: timeout snapshots the live graph state + run_state off
        # graph_meta (the bare asyncio.TimeoutError carries nothing), so a
        # subsequent resume reads true node status instead of falling back to
        # a full restart.
        assert meta.state_snapshot is not None
        assert meta.run_state is not None
        # The first node finished before the hang, so it is recorded done.
        assert "first" in meta.completed_nodes
