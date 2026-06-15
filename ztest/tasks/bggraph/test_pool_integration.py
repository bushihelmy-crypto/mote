#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end integration: ``BgGraph`` compiled task driven by the real pool.

Wires a compiled graph's ``poll`` coroutine through ``BackgroundTaskPool`` with
a ``TaskOutputStore`` progress sink, then asserts:

* the terminal ``BackgroundTaskNotification`` lands in the msg_buffer, and
* per-node ``report_progress`` events were appended to the task's disk output
  (the basis for ``<delta-summary>`` blocks).
"""
from __future__ import annotations

import pytest

from metagpt.common.schema import BackgroundTaskNotification, BgStatus, MessageQueue
from metagpt.tasks import BackgroundTaskPool, TaskOutputStore
from metagpt.tasks.bggraph import END, START, BgGraph

from .conftest import S, sync_node

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
    g.add_node("split", sync_node(lambda s: {"parts": 2}))
    g.add_node("tts", sync_node(lambda s: "audio"))
    g.add_node("render", sync_node(lambda s: "video"))
    g.add_node("merge", sync_node(lambda s: {"out": [s.tts, s.render]}))
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
        tid = pool.submit(res.poll, res.command_name, timeout=None, progress=True)
        await pool.wait_all()

        # Terminal notification reached the msg_buffer.
        msgs = await _drain_msgs(msg_buffer)
        notes = [m for m in msgs if isinstance(m, BackgroundTaskNotification)]
        assert len(notes) == 1
        assert notes[0].task_id == tid
        assert notes[0].status == BgStatus.SUCCESS

        # Meta is terminal/success.
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.SUCCESS

        # Per-node progress + terminal summary were appended to disk output.
        tail = await _flush(store, tid)
        assert "split" in tail
        assert "tts" in tail
        assert "render" in tail
        assert "merge" in tail
        assert "completed" in tail

    async def test_failure_notifies_failed(self, pool, store, msg_buffer):
        g = BgGraph("boomgraph", state_schema=S)

        def _raise(_s):
            raise ValueError("kaboom")

        g.add_node("a", sync_node(_raise))
        g.add_edge(START, "a")
        g.add_edge("a", END)

        res = await g.compile()(x=0)
        tid = pool.submit(res.poll, res.command_name, timeout=None, progress=True)
        await pool.wait_all()

        msgs = await _drain_msgs(msg_buffer)
        notes = [m for m in msgs if isinstance(m, BackgroundTaskNotification)]
        assert len(notes) == 1
        assert notes[0].status == BgStatus.FAILED

        # Disk output captured the node-failure + terminal-failed summary.
        tail = await _flush(store, tid)
        assert "kaboom" in tail

    async def test_progress_disabled_no_store_write(self, pool, store, msg_buffer):
        """Without ``progress=True`` the graph still runs, but no disk sink is set."""
        g = _media_graph()
        res = await g.compile()(x=0)
        tid = pool.submit(res.poll, res.command_name, timeout=None)
        await pool.wait_all()

        msgs = await _drain_msgs(msg_buffer)
        notes = [m for m in msgs if isinstance(m, BackgroundTaskNotification)]
        assert len(notes) == 1
        assert notes[0].status == BgStatus.SUCCESS
        # No output was initialised for this task.
        with pytest.raises(KeyError):
            await store.get_tail(tid)
