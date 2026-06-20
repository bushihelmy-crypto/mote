"""Unit tests for resume_tasks and cancel_tasks tools.

Uses real BackgroundTaskPool + BgGraph to verify the tools interact correctly
with the pool's resubmit/cancel/get_task_info and graph's resume methods.
"""
from __future__ import annotations

import asyncio

import pytest

from metagpt.executor.tasks.bggraph import BgGraph, GraphState, Stage, START, END
from metagpt.executor.tasks.pool import BackgroundTaskPool
from metagpt.executor.tasks.types import BgStatus, BgTaskResult, GraphMeta
from metagpt.executor.tools.resume_tasks import ResumeTasks
from metagpt.executor.tools.cancel_tasks import CancelTasks
from metagpt.executor.tool_result import ToolError
from metagpt.common.schema import MessageQueue

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class SimpleState(GraphState):
    x: int = 0


def sync_node(fn, *, field=None):
    async def node(state):
        async def submit():
            result = fn(state)
            return {field: result} if field is not None else result
        return Stage(submit=submit())
    return node


def boom_node():
    async def node(state):
        async def submit():
            raise ValueError("permanent failure")
        return Stage(submit=submit())
    return node


@pytest.fixture
def pool():
    buf = MessageQueue()
    return BackgroundTaskPool(buf, max_concurrency=10)


def _build_linear_graph():
    """a → b → END. a doubles x, b adds 10."""
    g = BgGraph("linear", state_schema=SimpleState, recursion_limit=10)
    g.add_node("a", sync_node(lambda s: s.x * 2, field="a"))
    g.add_node("b", sync_node(lambda s: s.a + 10, field="b"))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g


def _build_failing_graph():
    """a succeeds, b always fails."""
    g = BgGraph("failing", state_schema=SimpleState, recursion_limit=10)
    g.add_node("a", sync_node(lambda s: s.x * 2, field="a"))
    g.add_node("b", boom_node())
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g


def _make_resume_tool(pool):
    t = ResumeTasks()
    t.get_bg_pool = lambda: pool
    return t


def _make_cancel_tool(pool):
    t = CancelTasks()
    t.get_bg_pool = lambda: pool
    return t


# ---------------------------------------------------------------------------
# ResumeTasks tests
# ---------------------------------------------------------------------------


class TestResumeTasks:
    async def test_unknown_task_raises(self, pool):
        tool = _make_resume_tool(pool)
        with pytest.raises(ToolError, match="Unknown task_id"):
            await tool.call(task_id="bg_999")

    async def test_already_done_task(self, pool):
        g = _build_linear_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(
            res.poll_factory, res.command_name, timeout=5,
            graph_meta=GraphMeta(graph_ref=g, initial_params={"x": 5}, factory=executor),
        )
        # Wait for completion
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.SUCCESS

        tool = _make_resume_tool(pool)
        result = await tool.call(task_id=tid)
        assert "already" in result

    async def test_resume_from_node_after_failure(self, pool):
        g = _build_failing_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(
            res.poll_factory, res.command_name, timeout=10,
            graph_meta=GraphMeta(graph_ref=g, initial_params={"x": 5}, factory=executor),
            max_restarts=3,
        )
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.FAILED

        # Save state snapshot (normally done by _on_done for graph failures)
        # For this test, manually set it since our simple pool doesn't auto-snapshot
        meta.state_snapshot = SimpleState(x=5)
        setattr(meta.state_snapshot, "a", 10)  # a completed with x*2=10

        # Now fix node b to succeed
        g._nodes["b"].fn = sync_node(lambda s: s.a + 100, field="b")

        tool = _make_resume_tool(pool)
        result = await tool.call(task_id=tid, from_node="b")
        assert "resumed" in result.lower()

        # Wait for the resumed task
        await pool.wait_all()
        meta2 = pool.get_task_info(tid)
        assert meta2.status == BgStatus.SUCCESS

    async def test_resume_max_restarts_exceeded(self, pool):
        g = _build_failing_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(
            res.poll_factory, res.command_name, timeout=5,
            graph_meta=GraphMeta(graph_ref=g, initial_params={"x": 5}, factory=executor),
            max_restarts=1,
        )
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        meta.state_snapshot = SimpleState(x=5)

        tool = _make_resume_tool(pool)
        # retry_count starts at 0, max_restarts=1, first resubmit sets retry_count=1
        # Then trying again should be blocked
        # First resume should work (retry_count=0 < max_restarts=1)
        g._nodes["b"].fn = sync_node(lambda s: 42, field="b")
        result = await tool.call(task_id=tid, from_node="b")
        assert "resumed" in result.lower()
        await pool.wait_all()

        # Now meta.retry_count == 1 == max_restarts, force another failure
        meta = pool.get_task_info(tid)
        meta.status = BgStatus.FAILED
        meta.state_snapshot = SimpleState(x=5)
        result = await tool.call(task_id=tid, from_node="b")
        assert "restart limit" in result.lower()

    async def test_invalid_node_name_raises(self, pool):
        g = _build_linear_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(
            res.poll_factory, res.command_name, timeout=5,
            graph_meta=GraphMeta(graph_ref=g, initial_params={"x": 5}, factory=executor),
        )
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        meta.status = BgStatus.FAILED
        meta.state_snapshot = SimpleState(x=5)

        tool = _make_resume_tool(pool)
        with pytest.raises(ToolError, match="not found"):
            await tool.call(task_id=tid, from_node="nonexistent")

    async def test_resume_with_overrides(self, pool):
        """overrides dict applies to graph state before resuming."""
        g = _build_linear_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(
            res.poll_factory, res.command_name, timeout=5,
            graph_meta=GraphMeta(graph_ref=g, initial_params={"x": 5}, factory=executor),
        )
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        meta.status = BgStatus.FAILED
        # Simulate partial state: a completed (x*2=10), b failed
        meta.state_snapshot = SimpleState(x=5)
        setattr(meta.state_snapshot, "a", 10)

        # Override x to 99 — node b reads state.a (from previous node) not x,
        # but x should still be updated on state
        tool = _make_resume_tool(pool)
        # Make b succeed (reads state.a + 10)
        g._nodes["b"].fn = sync_node(lambda s: s.a + 10, field="b")
        result = await tool.call(task_id=tid, from_node="b", overrides={"x": 99})
        assert "resumed" in result.lower()

        # Verify override was applied to state
        assert meta.state_snapshot.x == 99

    async def test_resume_from_node_with_unmet_upstream_raises(self, pool):
        """Resuming from a node whose declared upstream isn't completed is infeasible.

        a → b → c, where c declares an input sourced from b. The graph fails at
        b, so b never completes. Resuming from c alone (without re-running or
        skipping b) would run c with a missing input, so the tool rejects it.
        """
        g = BgGraph("chain", state_schema=SimpleState, recursion_limit=10)
        g.add_node("a", sync_node(lambda s: s.x * 2, field="a"))
        g.add_node("b", boom_node())
        g.add_node("c", sync_node(lambda s: 1, field="c"), params={"in": {"from": "b"}})
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", END)

        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(
            res.poll_factory, res.command_name, timeout=10,
            graph_meta=res.graph_meta, max_restarts=3,
        )
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.FAILED

        tool = _make_resume_tool(pool)
        with pytest.raises(ToolError, match="Cannot resume from"):
            await tool.call(task_id=tid, from_node="c")

        # Re-running b alongside c satisfies the dependency — no longer infeasible.
        g._nodes["b"].fn = sync_node(lambda s: s.a + 10, field="b")
        result = await tool.call(task_id=tid, from_node=["b", "c"])
        assert "resumed" in result.lower()

    async def test_resume_unknown_override_key_raises(self, pool):
        """A mistyped override key is rejected, not silently dropped."""
        g = _build_failing_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(
            res.poll_factory, res.command_name, timeout=10,
            graph_meta=res.graph_meta, max_restarts=3,
        )
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.FAILED
        assert meta.state_snapshot is not None  # captured on failure

        tool = _make_resume_tool(pool)
        g._nodes["b"].fn = sync_node(lambda s: s.a + 1, field="b")
        with pytest.raises(ToolError, match="Unknown override key"):
            await tool.call(task_id=tid, from_node="b", overrides={"bogus": 1})

        # The declared input field 'x' is accepted.
        result = await tool.call(task_id=tid, from_node="b", overrides={"x": 99})
        assert "resumed" in result.lower()
        assert meta.state_snapshot.x == 99

    async def test_resume_full_restart_with_overrides(self, pool):
        """Full restart merges overrides into initial_params."""
        g = _build_linear_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(
            res.poll_factory, res.command_name, timeout=5,
            graph_meta=GraphMeta(graph_ref=g, initial_params={"x": 5}, factory=executor),
        )
        await pool.wait_all()
        meta = pool.get_task_info(tid)
        meta.status = BgStatus.FAILED
        # No state_snapshot and no graph_ref → triggers factory restart path
        meta.state_snapshot = None
        meta.graph_meta.graph_ref = None

        tool = _make_resume_tool(pool)
        result = await tool.call(task_id=tid, overrides={"x": 42})
        assert "resumed" in result.lower()

        # Wait for completion — factory was called with merged {x: 42}
        await pool.wait_all()
        meta2 = pool.get_task_info(tid)
        assert meta2.status == BgStatus.SUCCESS


# ---------------------------------------------------------------------------
# CancelTasks tests
# ---------------------------------------------------------------------------


class TestCancelTasks:
    async def test_unknown_task_raises(self, pool):
        tool = _make_cancel_tool(pool)
        with pytest.raises(ToolError, match="Unknown task_id"):
            await tool.call(task_id="bg_999")

    async def test_cancel_running_task(self, pool):
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return "done"

        tid = pool.submit(lambda: slow(), "slow-task", timeout=None)
        await asyncio.sleep(0)  # let task start

        tool = _make_cancel_tool(pool)
        result = await tool.call(task_id=tid, reason="no longer needed")
        assert "cancelled" in result.lower()
        assert "no longer needed" in result

    async def test_cancel_already_done(self, pool):
        async def instant():
            return "done"

        tid = pool.submit(lambda: instant(), "fast-task", timeout=5)
        await pool.wait_all()

        tool = _make_cancel_tool(pool)
        result = await tool.call(task_id=tid)
        assert "already" in result.lower()

    async def test_cancel_with_default_reason(self, pool):
        gate = asyncio.Event()

        async def slow():
            await gate.wait()

        tid = pool.submit(lambda: slow(), "pending-task", timeout=None)
        await asyncio.sleep(0)

        tool = _make_cancel_tool(pool)
        result = await tool.call(task_id=tid)
        assert "user requested" in result
