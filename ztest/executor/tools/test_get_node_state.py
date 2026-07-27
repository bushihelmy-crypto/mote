"""Tests for the GetNodeState tool — read layer over GraphRunState.

Verifies the tool reports per-node status / attempts / failure reason / running
node for graph tasks, and degrades gracefully for unknown or non-graph tasks.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.contracts.schema import MessageQueue
from mote.orchestration.tasks.bggraph import END, START, BgGraph, GraphState, Stage
from mote.orchestration.tasks.pool import BackgroundTaskPool
from mote.orchestration.tasks.types import BgStatus
from mote.product.toolsets.builtin.get_node_state import GetNodeState
from mote.runtime.tools.tool_result import ToolError

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


class RingState(GraphState):
    """State for a self-looping ring: ``n`` counts down one lap at a time."""

    n: int = 0


def _ring_graph():
    """A ``work`` node that self-loops until ``n`` hits 0 (mirrors review_batch).

    Each lap decrements ``n`` and the conditional router sends it back to itself
    while ``n`` remains, else on to ``done``. The node's ``attempts`` therefore
    equals the number of laps (batches), NOT a retry count.
    """
    g = BgGraph("ring", state_schema=RingState, recursion_limit=50)

    async def work(state):
        async def submit():
            return {"n": max(0, state.n - 1)}

        return Stage(submit=submit())

    g.add_node("work", work)
    g.add_node("done", sync_node(lambda s: {"n": s.n}))
    g.add_edge(START, "work")
    g.add_conditional_edges(
        "work",
        lambda s: "loop" if s.n else "done",
        {"loop": "work", "done": "done"},
    )
    g.add_edge("done", END)
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
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
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
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid, nodes=["b"])
        # Only b's detail block: status + declared input sourced from field 'a'.
        assert "Node 'b'" in result
        assert "inputs:" in result
        assert "val" in result
        assert "state field 'a'" in result
        assert "[int]" in result
        assert "the doubled x" in result
        # b's input value (field 'a' = doubled x = 10) is previewed inline.
        assert "= 10" in result
        # b failed, so it recorded no writes → the output line says so.
        assert "no state fields recorded yet" in result
        # Detail mode is scoped: a's own detail block is not rendered.
        assert "Node 'a'" not in result

    async def test_detail_mode_reports_output_writes_and_consumers(self, pool):
        g = _detail_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid, nodes=["a"])
        # a wrote field 'a' (value 10), consumed by b's 'val' input.
        assert "output: writes a" in result
        assert "a = 10" in result
        assert "consumed by: b.val" in result

    async def test_consumers_resolve_across_renamed_fields(self, pool):
        """A node writing a differently-named channel still links to consumers."""
        g = BgGraph("renamed", state_schema=SimpleState, recursion_limit=10)
        # producer writes field 'data' (name != node name); consumer reads it.
        g.add_node("producer", sync_node(lambda s: {"data": s.x + 1}))
        g.add_node(
            "consumer",
            sync_node(lambda s: {"x": 0}),
            params={"d": {"from": "data", "type": int, "desc": "produced data"}},
        )
        g.add_edge(START, "producer")
        g.add_edge("producer", "consumer")
        g.add_edge("consumer", END)
        res = await g.compile()(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid, nodes=["producer"])
        assert "output: writes data" in result
        assert "data = 6" in result
        # The field/channel-aware consumer resolution finds the link.
        assert "consumed by: consumer.d" in result

    async def test_fields_mode_dumps_state_values(self, pool):
        g = _detail_graph()
        res = await g.compile()(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid, fields=["a", "x"])
        assert "state fields:" in result
        assert "a:" in result
        assert "10" in result
        assert "x:" in result
        assert "5" in result

    async def test_fields_mode_unknown_field_raises(self, pool):
        g = _detail_graph()
        res = await g.compile()(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        with pytest.raises(ToolError, match="Unknown state field"):
            await tool.call(task_id=tid, fields=["nonexistent"])

    async def test_fields_as_json_string_rejected(self, pool):
        # The model must pass a real list; a stringified array (native tool-use
        # sometimes serializes it) is rejected with a clear message instead of
        # being mistaken for one bogus field name.
        g = _detail_graph()
        res = await g.compile()(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        with pytest.raises(ToolError, match="Expected a list, got a JSON string"):
            await tool.call(task_id=tid, fields='["a", "x"]')

    async def test_detail_mode_unknown_node_raises(self, pool):
        g = _detail_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        with pytest.raises(ToolError, match="not found"):
            await tool.call(task_id=tid, nodes=["nonexistent"])

    async def test_overview_labels_self_loop_activations_not_retries(self, pool):
        # A ring node that loops 3 times must NOT read as a retrying/stalled
        # node: its attempts are laps, not failed retries. This is the
        # observability fix for review_batch being misread as "restarted".
        g = _ring_graph()
        res = await g.compile()(n=3)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid)
        assert "work: success" in result
        # The ring node is labelled as activations/laps, explicitly not retries.
        assert "activations" in result
        assert "not retries" in result
        # It must NOT use the plain "attempts N" wording that reads as retries.
        assert "work: success (attempts" not in result

    async def test_detail_mode_labels_self_loop_activations(self, pool):
        g = _ring_graph()
        res = await g.compile()(n=2)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        result = await tool.call(task_id=tid, nodes=["work"])
        assert "Node 'work'" in result
        assert "activations" in result
        assert "not retries" in result


class TestConsumeMarksRetrieved:
    """A successful inspect consumes the push-once result: it flips ``retrieved``
    and retires the re-projected pointer. An error path (raise) does not.
    """

    async def test_successful_overview_marks_retrieved_and_retires(self, pool):
        retired: list[str] = []
        pool.set_retire_result(retired.append)

        g = _failing_graph()
        res = await g.compile()(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        await tool.call(task_id=tid)  # successful overview

        meta = pool.get_task_info(tid)
        # meta reaped after consume (retrieved + not running + terminal), so the
        # retire callback is the observable signal that the consume fired.
        assert retired == [tid]
        assert meta is None or meta.retrieved is True

    async def test_error_path_does_not_mark_retrieved(self, pool):
        retired: list[str] = []
        pool.set_retire_result(retired.append)

        g = _detail_graph()
        res = await g.compile()(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        tool = _make_tool(pool)
        with pytest.raises(ToolError, match="Unknown state field"):
            await tool.call(task_id=tid, fields=["nonexistent"])

        # The failed consume must NOT retire the pointer nor flip retrieved.
        assert retired == []
        assert pool.get_task_info(tid).retrieved is False
