"""Tests for the authoritative per-node run state (GraphRunState).

Covers the unit behaviour of GraphRunState (status transitions, attempt
accumulation, completed-name derivation, legacy inference) and its end-to-end
recording through the engine + pool (success / failure / resume).
"""
from __future__ import annotations

import pytest

from mote.contracts.schema import MessageQueue
from mote.orchestration.tasks.bggraph import END, START, BgGraph, GraphState, Stage
from mote.orchestration.tasks.bggraph.types import GraphRunState, NodeRecord
from mote.orchestration.tasks.pool import BackgroundTaskPool
from mote.orchestration.tasks.types import BgStatus

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / helpers
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
    return BackgroundTaskPool(MessageQueue(), max_concurrency=10)


def _linear_graph():
    g = BgGraph("linear", state_schema=SimpleState, recursion_limit=10)
    g.add_node("a", sync_node(lambda s: s.x * 2, field="a"))
    g.add_node("b", sync_node(lambda s: s.a + 10, field="b"))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g


def _failing_graph():
    g = BgGraph("failing", state_schema=SimpleState, recursion_limit=10)
    g.add_node("a", sync_node(lambda s: s.x * 2, field="a"))
    g.add_node("b", boom_node())
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g


# ---------------------------------------------------------------------------
# GraphRunState unit behaviour
# ---------------------------------------------------------------------------


class TestGraphRunStateUnit:
    def test_for_graph_seeds_pending_records(self):
        g = _linear_graph()
        rs = GraphRunState.for_graph(g)
        assert set(rs.records) == {"a", "b"}
        assert all(r.status == BgStatus.PENDING for r in rs.records.values())
        assert rs.completed_names() == set()

    def test_mark_running_accumulates_attempts(self):
        rs = GraphRunState(records={"a": NodeRecord(name="a")})
        rs.mark_running("a")
        assert rs.records["a"].attempts == 1
        assert rs.records["a"].status == BgStatus.RUNNING
        # A second run (e.g. after resume) keeps counting up — retry budget.
        rs.mark_running("a")
        assert rs.records["a"].attempts == 2

    def test_completed_names_includes_success_and_skipped(self):
        rs = GraphRunState.for_graph(_linear_graph())
        rs.mark_success("a")
        rs.mark_skipped("b")
        assert rs.completed_names() == {"a", "b"}

    def test_mark_failed_records_full_error(self):
        rs = GraphRunState.for_graph(_failing_graph())
        rs.mark_failed("b", ValueError("boom details"))
        assert rs.records["b"].status == BgStatus.FAILED
        assert rs.records["b"].last_error == "boom details"

    def test_reset_preserves_attempts_clears_error(self):
        rs = GraphRunState.for_graph(_failing_graph())
        rs.mark_running("b")
        rs.mark_failed("b", ValueError("boom"))
        rs.reset("b")
        rec = rs.records["b"]
        assert rec.status == BgStatus.PENDING
        assert rec.attempts == 1  # preserved across reset
        assert rec.last_error is None

    def test_running_names(self):
        rs = GraphRunState.for_graph(_linear_graph())
        rs.mark_running("a")
        assert rs.running_names() == ["a"]
        rs.mark_success("a")
        assert rs.running_names() == []

    def test_infer_from_state_returns_all_pending(self):
        # Per-node value inference is gone: the run-state is authoritative and
        # always carried live, so ``infer_from_state`` just seeds all-PENDING
        # records (delegating to ``for_graph``) regardless of state contents.
        g = _linear_graph()
        state = SimpleState(x=5)
        setattr(state, "a", 10)  # field set, but no longer implies completion
        rs = GraphRunState.infer_from_state(g, state)
        assert rs.completed_names() == set()


# ---------------------------------------------------------------------------
# End-to-end recording through engine + pool
# ---------------------------------------------------------------------------


class TestRunStateRecording:
    async def test_success_records_all_nodes(self, pool):
        g = _linear_graph()
        executor = g.compile()
        res = await executor(x=5)
        # Submit with the engine's own graph_meta (carries the shared run_state).
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        rs = pool.get_run_state(tid)
        assert rs is not None
        assert rs.completed_names() == {"a", "b"}
        assert rs.records["a"].attempts == 1

    async def test_failure_snapshots_run_state_and_state(self, pool):
        g = _failing_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        meta = pool.get_task_info(tid)
        assert meta.status == BgStatus.FAILED
        # State snapshot captured on failure (not only on LLM pause).
        assert meta.state_snapshot is not None
        assert getattr(meta.state_snapshot, "a", None) == 10
        rs = pool.get_run_state(tid)
        assert rs.records["a"].status == BgStatus.SUCCESS
        assert rs.records["b"].status == BgStatus.FAILED
        assert "permanent failure" in rs.records["b"].last_error
        # completed_nodes mirrors run_state's authoritative completed set.
        assert meta.completed_nodes == {"a"}

    async def test_resume_preserves_attempt_budget(self, pool):
        """Re-running a failed node accumulates attempts across resume."""
        g = _failing_graph()
        executor = g.compile()
        res = await executor(x=5)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta, max_restarts=3)
        await pool.wait_all()

        rs = pool.get_run_state(tid)
        attempts_before = rs.records["b"].attempts
        assert attempts_before >= 1

        # Fix b, resume from it; the same run_state is reused so b's prior
        # attempts are preserved and the new run adds to them.
        g._nodes["b"].fn = sync_node(lambda s: s.a + 100, field="b")
        from mote.product.toolsets.builtin.resume_tasks import ResumeTasks

        tool = ResumeTasks()
        tool.get_bg_pool = lambda: pool
        await tool.call(task_id=tid, from_node="b")
        await pool.wait_all()

        rs2 = pool.get_run_state(tid)
        assert rs2.records["b"].status == BgStatus.SUCCESS
        # attempts kept climbing (not reset to 0 on resume).
        assert rs2.records["b"].attempts == attempts_before + 1


# ---------------------------------------------------------------------------
# Conditional route-key recording (observability)
# ---------------------------------------------------------------------------


def _branch_graph(threshold: int):
    """a → router(big|small) → big|small → END. Router keys on s.a vs threshold."""
    g = BgGraph("branch", state_schema=SimpleState, recursion_limit=10)
    g.add_node("a", sync_node(lambda s: s.x, field="a"))
    g.add_node("big", sync_node(lambda s: "BIG", field="big"))
    g.add_node("small", sync_node(lambda s: "SMALL", field="small"))
    g.add_edge(START, "a")
    g.add_conditional_edges(
        "a",
        lambda s, t=threshold: "big" if s.a > t else "small",
        {"big": "big", "small": "small"},
    )
    g.add_edge("big", END)
    g.add_edge("small", END)
    return g


class TestRouteKeyRecording:
    async def test_conditional_route_key_recorded(self, pool):
        # x=20 > 10 → router picks 'big'.
        g = _branch_graph(threshold=10)
        executor = g.compile()
        res = await executor(x=20)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        rs = pool.get_run_state(tid)
        assert rs.records["a"].last_route_key == "big"

    async def test_conditional_route_key_other_branch(self, pool):
        # x=3 <= 10 → router picks 'small'.
        g = _branch_graph(threshold=10)
        executor = g.compile()
        res = await executor(x=3)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        rs = pool.get_run_state(tid)
        assert rs.records["a"].last_route_key == "small"

    async def test_non_routed_node_has_no_route_key(self, pool):
        g = _branch_graph(threshold=10)
        executor = g.compile()
        res = await executor(x=20)
        tid = pool.submit(res.poll_factory, res.command_name, timeout=10, graph_meta=res.graph_meta)
        await pool.wait_all()

        rs = pool.get_run_state(tid)
        # 'big' executed but routes only via a plain edge → no route key.
        assert rs.records["big"].last_route_key is None
