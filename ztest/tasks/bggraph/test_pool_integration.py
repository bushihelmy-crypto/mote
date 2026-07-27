#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end integration: ``BgGraph`` compiled task driven by the real pool.

Wires a compiled graph's ``poll`` coroutine through ``BackgroundTaskPool`` with
a ``TaskOutputStore`` progress sink, then asserts:

* only decision-point notifications reach the msg_buffer — the whole-task
  terminal (END, via pool._on_done), a route pause, and a node failure. START
  and per-node completions are disk-only (no telemetry publication), and
* per-node ``report_progress`` events were appended to the task's disk output
  (the basis for ``<delta-summary>`` blocks).
"""
from __future__ import annotations

import asyncio

import pytest

from mote.contracts.schema import MessageQueue
from mote.orchestration.tasks import BackgroundTaskNotification, BackgroundTaskPool, BgStatus, TaskOutputStore
from mote.orchestration.tasks.bggraph import END, START, BgGraph

from .conftest import S, gated_node, sync_node

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

        # Only the whole-task terminal is pushed to the model: START and every
        # per-node completion are disk-only now (progress, not decisions), and
        # the one terminal SUCCESS is produced by pool._on_done.
        msgs = await _drain_msgs(msg_buffer)
        notes = [m for m in msgs if isinstance(m, BackgroundTaskNotification)]
        assert len(notes) == 1
        assert notes[0].status == BgStatus.SUCCESS
        # Terminal notification contains "success" + the real task_id.
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
    """Pool correctly identifies a GraphPause and supports resubmit."""

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

        # Only the route pause is pushed (a decision point, via pool._on_done);
        # START + node completion are disk-only now.
        msgs = await _drain_msgs(msg_buffer)
        assert len(msgs) >= 1
        contents = " ".join(m.content for m in msgs)
        assert "waiting_for_route" in contents or "route" in contents.lower()

    async def test_deadlocked_join_sets_stalled(self, pool, store, msg_buffer):
        """A deadlocked AND-join surfaces as STALLED (not a spurious SUCCESS)."""
        never = asyncio.Event()  # deliberately never set
        g = BgGraph("stallpool", state_schema=S)
        g.add_node("entry", sync_node(lambda s: "e", field="entry"))
        g.add_node("a", sync_node(lambda s: "a", field="a"))
        g.add_node("b", gated_node(never, lambda s: "b", field="b"))
        g.add_node("c", sync_node(lambda s: "c", field="c"))
        g.add_edge(START, "entry")
        g.add_conditional_edges("entry", lambda s: "only_a", {"only_a": "a"})
        g.add_edge(["a", "b"], "c")
        g.add_edge("c", END)

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
        assert meta.status == BgStatus.STALLED
        # Snapshot saved for resume (like a route pause).
        assert meta.state_snapshot is not None
        assert "a" in meta.completed_nodes
        assert "c" not in meta.completed_nodes

        # The stall is pushed as a decision point (via pool._on_done) and names
        # the deadlocked join + its missing upstream.
        msgs = await _drain_msgs(msg_buffer)
        assert len(msgs) >= 1
        contents = " ".join(m.content for m in msgs)
        assert "stalled" in contents.lower()
        assert "c" in contents

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


class TestPoolResultLimitPolicy:
    """The pool applies the SAME ``ToolResultLimitConfig`` the sync ToolExecutor
    uses — to the success result AND every error/timeout/cancel block. A large
    error persists+previews (not blunt-truncates); the enable toggle passes it
    through whole.
    """

    async def test_large_error_persists_under_config(self, tmp_path, msg_buffer):
        from mote.contracts.schema import PERSISTED_OUTPUT_OPEN_TAG, ToolResultLimitConfig
        from mote.orchestration.tasks import BackgroundTaskPool, TaskOutputStore

        store = TaskOutputStore(base_dir=tmp_path)
        # Tiny cap so a modest error block trips the persist threshold.
        pool = BackgroundTaskPool(
            msg_buffer=msg_buffer,
            output_store=store,
            limit_config=ToolResultLimitConfig(default_max_result_size_chars=200),
        )

        g = BgGraph("bigboom", state_schema=S)

        def _raise(_s):
            raise ValueError("X" * 5000)

        g.add_node("a", sync_node(_raise))
        g.add_edge(START, "a")
        g.add_edge("a", END)

        res = await g.compile()(x=0)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=None)
        await pool.wait_all()

        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.FAILED
        # Over the cap → persisted+preview envelope, not a blunt slice.
        assert meta.result.startswith(PERSISTED_OUTPUT_OPEN_TAG)

    async def test_limit_disabled_passes_error_through_whole(self, tmp_path, msg_buffer):
        from mote.contracts.schema import PERSISTED_OUTPUT_OPEN_TAG, ToolResultLimitConfig
        from mote.orchestration.tasks import BackgroundTaskPool, TaskOutputStore

        store = TaskOutputStore(base_dir=tmp_path)
        pool = BackgroundTaskPool(
            msg_buffer=msg_buffer,
            output_store=store,
            limit_config=ToolResultLimitConfig(enable_tool_result_limit=False),
        )

        g = BgGraph("nolimit", state_schema=S)

        def _raise(_s):
            raise ValueError("Y" * 5000)

        g.add_node("a", sync_node(_raise))
        g.add_edge(START, "a")
        g.add_edge("a", END)

        res = await g.compile()(x=0)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=None)
        await pool.wait_all()

        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.FAILED
        # Disabled → whole error block, never persisted.
        assert not meta.result.startswith(PERSISTED_OUTPUT_OPEN_TAG)
        assert "YYYY" in meta.result


def _wire_registry(pool):
    """Wire a real ResourceRegistry into *pool* the way ``_build_bg_pool`` does.

    Registers a ``task_result`` pointer on every terminal / pause via the
    late-bound ``on_terminal_result`` callback, and retires it via the
    ``retire_result`` callback. Returns the registry so a test can assert what
    survived compaction.
    """
    from mote.orchestration.tasks.status import PAUSE_STATUSES, TERMINAL_STATUSES
    from mote.runtime.resources import ResourceRegistry, build_task_result_pointer

    registry = ResourceRegistry()

    def _on_terminal(meta) -> None:
        status_value = meta.status.value if isinstance(meta.status, BgStatus) else str(meta.status)
        if meta.status in PAUSE_STATUSES:
            content = build_task_result_pointer(
                task_id=meta.task_id,
                command_name=meta.command_name,
                status=status_value,
                summary=f"{meta.command_name} paused ({status_value}), awaiting a decision.",
            )
        elif meta.status in TERMINAL_STATUSES:
            content = build_task_result_pointer(
                task_id=meta.task_id,
                command_name=meta.command_name,
                status=status_value,
                summary=f"{meta.command_name} finished ({status_value}).",
                result=meta.result,
                output_path=meta.output_path,
            )
        else:
            return
        registry.load(id=meta.task_id, kind="task_result", content=content, sticky=True)
        meta.registered_resource = True

    pool.set_on_terminal_result(_on_terminal)
    pool.set_retire_result(registry.unload)
    return registry


class TestPushOnceResultRegistration:
    """The pool's late-bound terminal callback registers a push-once result as a
    ``task_result`` ResourceUnit (so it re-projects after compaction); consuming
    it (``mark_retrieved``) retires the unit and reaps the meta; a pause registers
    a resume-marker that survives mere inspection.
    """

    async def test_success_terminal_registers_task_result_pointer(self, pool, store, msg_buffer):
        registry = _wire_registry(pool)
        g = _media_graph()
        res = await g.compile()(x=0)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=None, progress=True)
        await pool.wait_all()

        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.SUCCESS
        assert meta.registered_resource is True
        # The registry now carries a re-projectable pointer for this task.
        assert tid in registry
        (m,) = registry.project()
        assert m.resource_kind == "task_result"
        assert m.resource_id == tid
        assert "<task-result>" in m.content
        assert "success" in m.content

    async def test_pause_registers_resume_marker(self, pool, store, msg_buffer):
        registry = _wire_registry(pool)
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
        assert tid in registry
        (m,) = registry.project()
        # A pause marker has a resume-hint and no produced result body.
        assert "<resume-hint>" in m.content
        assert "<result>" not in m.content

    async def test_mark_retrieved_retires_pointer_and_reaps_meta(self, pool, store, msg_buffer):
        registry = _wire_registry(pool)
        g = _media_graph()
        res = await g.compile()(x=0)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=None, progress=True)
        await pool.wait_all()
        assert tid in registry

        # The model consumes the result (e.g. via GetNodeState / cancel).
        pool.mark_retrieved(tid)

        # Pointer retired (stops re-surfacing) and meta reaped (bounded _meta).
        assert tid not in registry
        assert registry.project() == []
        assert pool.get_task_info(tid) is None

    async def test_paused_task_not_reaped_even_after_mark_retrieved(self, pool, store, msg_buffer):
        registry = _wire_registry(pool)
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
        assert pool.get_task_info(tid).status == BgStatus.WAITING_FOR_ROUTE

        # Inspection is not a consume for a pause: the resume marker + meta must
        # survive so resume_tasks can still act on it.
        pool.mark_retrieved(tid)

        assert pool.get_task_info(tid) is not None
        assert tid in registry  # resume marker still re-projects

    async def test_resume_retires_stale_marker_and_reregisters_on_next_terminal(self, pool, store, msg_buffer):
        registry = _wire_registry(pool)
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
        assert tid in registry  # pause marker registered

        async def resumed():
            return "resumed-result"

        # resubmit retires the stale pause marker and resets the register guard.
        pool.resubmit(tid, lambda: resumed(), progress=False)
        assert tid not in registry  # stale marker retired on resubmit
        await pool.wait_all()

        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.SUCCESS
        # The fresh terminal re-registers a new result pointer under same id.
        assert tid in registry
        (m,) = registry.project()
        assert "resumed-result" in m.content
