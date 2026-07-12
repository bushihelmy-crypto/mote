"""Tests for the GetNodeState tool — read layer over GraphRunState.

Verifies the tool reports per-node status / attempts / failure reason / running
node for graph tasks, and degrades gracefully for unknown or non-graph tasks.
"""
from __future__ import annotations

import asyncio

import pytest

from metagpt.executor.tasks.bggraph import BgGraph, GraphState, Stage, START, END
from metagpt.executor.tasks.pool import BackgroundTaskPool
from metagpt.executor.tasks.types import BgStatus
from metagpt.executor.tools.get_node_state import GetNodeState
from metagpt.executor.tool_result import ToolError
from metagpt.common.schema import MessageQueue

pytestmark = pytest.mark.asyncio


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
    return BackgroundTaskPool(MessageQueue(), max_concurrency=10)


def _failing_graph():
    g = BgGraph("failing", state_schema=SimpleState, recursion_limit=10)
    g.add_node("a", sync_node(lambda s: s.x * 2, field="a"))
    g.add_node("b", boom_node())
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g


def _detail_graph():
    """a doubles x; b consumes a's output (declared param) then fails."""
    g = BgGraph("detail", state_schema=SimpleState, recursion_limit=10)
    g.add_node("a", sync_node(lambda s: s.x * 2, field="a"))
    g.add_node(
        "b",
        boom_node(),
        params={"val": {"from": "a", "type": int, "desc": "the doubled x"}},
    )
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g


def _make_tool(pool):
    t = GetNodeState()
    t.get_bg_pool = lambda: pool
    return t


class TestGetNodeState:
    async def test_unknown_task_raises(self, pool):
        tool = _make_tool(pool)
        with pytest.raises(ToolError, match="Unknown task_id"):
            await tool.call(task_id="bg_999")

    async def test_non_graph_task_has_no_per_node_state(self, pool):
        async def instant():
            return "done"

        tid = pool.submit(lambda: instant(), "plain-task", timeout=5)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid)
        assert "no per-node" in result.lower()

    async def test_overview_reports_node_statuses(self, pool):
        g = _failing_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10,
                          graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid)
        # a succeeded, b failed with the full error and an attempt count.
        assert "a: success" in result
        assert "b: failed" in result
        assert "permanent failure" in result
        assert "attempts" in result
        assert "running:" in result

    async def test_detail_mode_shows_description_inputs_output(self, pool):
        g = _detail_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10,
                          graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid, nodes=["b"])
        # Only b's detail block: status + declared input sourced from a's output.
        assert "Node 'b'" in result
        assert "inputs:" in result
        assert "val" in result
        assert "from 'a' output" in result
        assert "[int]" in result
        assert "the doubled x" in result
        assert "output: writes to state" in result
        # Detail mode is scoped: a's own detail block is not rendered.
        assert "Node 'a'" not in result

    async def test_detail_mode_reports_output_consumers(self, pool):
        g = _detail_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10,
                          graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid, nodes=["a"])
        # a's output is consumed by b's 'val' input.
        assert "output: writes to state" in result
        assert "consumed by: b.val" in result

    async def test_detail_mode_unknown_node_raises(self, pool):
        g = _detail_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10,
                          graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        with pytest.raises(ToolError, match="not found"):
            await tool.call(task_id=tid, nodes=["nonexistent"])
