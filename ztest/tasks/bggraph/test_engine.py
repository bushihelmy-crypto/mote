#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the bggraph frontier scheduler (:mod:`metagpt.executor.bggraph.engine`).

Covers linear chains, parallel fan-out, waiting-edge AND-joins (fast + slow
source), conditional routing, cycles bounded by ``recursion_limit``, independent
parallel failure, auto-retries, and the LLM-route pause sentinel.

``report_progress`` is a no-op outside a progress context, so the driver coroutine
(``BgTaskResult.poll``) can be awaited directly without a pool / disk sink.
"""
from __future__ import annotations

import asyncio

import pytest

from metagpt.executor.tasks.types import BgTaskResult
from metagpt.executor.tasks.bggraph import (
    END,
    START,
    GraphBatchFailureError,
    BgGraph,
    GraphRecursionError,
)
from metagpt.executor.tasks.bggraph.types import LlmPauseResult, _LLM_ROUTE_SENTINEL

from .conftest import S, boom_node, flaky_node, gated_node, non_retryable_flaky_node, sync_node

pytestmark = pytest.mark.asyncio


async def _run(graph: BgGraph, **inputs):
    res = await graph.compile()(**inputs)
    assert isinstance(res, BgTaskResult)
    return await res.poll


class TestLinear:
    async def test_single_node(self):
        g = BgGraph("lin1", state_schema=S)
        g.add_node("a", sync_node(lambda s: s.x + 1))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        assert await _run(g, x=41) == 42

    async def test_chain(self):
        g = BgGraph("chain", state_schema=S)
        g.add_node("a", sync_node(lambda s: s.x + 1))
        g.add_node("b", sync_node(lambda s: s.a * 2))
        g.add_node("c", sync_node(lambda s: s.b + 10))
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", END)
        # x=0 → a=1 → b=2 → c=12
        assert await _run(g, x=0) == 12


class TestFanOut:
    async def test_parallel_then_join(self):
        g = BgGraph("fan", state_schema=S)
        g.add_node("a", sync_node(lambda s: s.x + 1))
        g.add_node("tts", sync_node(lambda s: s.a * 10))
        g.add_node("render", sync_node(lambda s: s.a * 100))
        g.add_node("merge", sync_node(lambda s: {"v": s.tts + s.render}))
        g.add_edge(START, "a")
        g.add_edge("a", "tts")
        g.add_edge("a", "render")
        g.add_edge(["tts", "render"], "merge")
        g.add_edge("merge", END)
        # x=0 → a=1 → tts=10, render=100 → merge=110
        assert await _run(g, x=0) == {"v": 110}

    async def test_waiting_edge_fast_and_slow_source(self):
        """AND-join must wait for the *slow* source before firing the merge."""
        slow = asyncio.Event()
        g = BgGraph("waitslow", state_schema=S)
        g.add_node("a", sync_node(lambda s: 1))
        g.add_node("fast", sync_node(lambda s: "fast"))
        g.add_node("slow", gated_node(slow, lambda s: "slow"))
        g.add_node("merge", sync_node(lambda s: [s.fast, s.slow]))
        g.add_edge(START, "a")
        g.add_edge("a", "fast")
        g.add_edge("a", "slow")
        g.add_edge(["fast", "slow"], "merge")
        g.add_edge("merge", END)

        res = await g.compile()(x=0)
        task = asyncio.ensure_future(res.poll)
        # Let the fast source settle; merge must NOT have fired yet.
        await asyncio.sleep(0.02)
        assert not task.done()
        slow.set()
        assert await task == ["fast", "slow"]


class TestConditional:
    async def test_routes_on_post_state(self):
        g = BgGraph("cond", state_schema=S)
        g.add_node("a", sync_node(lambda s: s.x))
        g.add_node("big", sync_node(lambda s: "BIG"))
        g.add_node("small", sync_node(lambda s: "SMALL"))
        g.add_edge(START, "a")
        g.add_conditional_edges(
            "a", lambda s: "big" if s.a > 10 else "small", {"big": "big", "small": "small"}
        )
        g.add_edge("big", END)
        g.add_edge("small", END)
        assert await _run(g, x=20) == {"big": "BIG"}
        assert await _run(g, x=3) == {"small": "SMALL"}


class TestCycle:
    async def test_recursion_limit_exceeded(self):
        g = BgGraph("cyc", state_schema=S, recursion_limit=5)
        g.add_node("a", sync_node(lambda s: (getattr(s, "b", None) or 0) + 1))
        g.add_node("b", sync_node(lambda s: (s.a or 0) + 1))
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_conditional_edges("b", lambda s: "loop", {"loop": "a", "done": END})
        with pytest.raises(GraphRecursionError):
            await _run(g, x=0)

    async def test_bounded_cycle_completes(self):
        """A cycle that exits before the limit returns normally."""
        g = BgGraph("cyc2", state_schema=S, recursion_limit=50)
        g.add_node("a", sync_node(lambda s: (getattr(s, "a", None) or 0) + 1))
        g.add_edge(START, "a")
        # Loop back to 'a' until its result reaches 3, then go to END.
        g.add_conditional_edges(
            "a", lambda s: "done" if s.a >= 3 else "loop", {"loop": "a", "done": END}
        )
        assert await _run(g, x=0) == 3


class TestFailure:
    async def test_single_failure_raises_batch(self):
        g = BgGraph("fail", state_schema=S)
        g.add_node("a", boom_node(ValueError("nope")))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        with pytest.raises(GraphBatchFailureError) as ei:
            await _run(g, x=0)
        assert [n for n, _ in ei.value.failures] == ["a"]

    async def test_parallel_failure_isolated_rest_continue(self):
        """One branch fails; the independent branch still completes."""
        g = BgGraph("failpar", state_schema=S)
        g.add_node("a", sync_node(lambda s: 1))
        g.add_node("bad", boom_node(ValueError("bad branch")))
        g.add_node("good", sync_node(lambda s: "good-done"))
        g.add_edge(START, "a")
        g.add_edge("a", "bad")
        g.add_edge("a", "good")
        g.add_edge("good", END)
        with pytest.raises(GraphBatchFailureError) as ei:
            await _run(g, x=0)
        assert [n for n, _ in ei.value.failures] == ["bad"]


@pytest.fixture
def fast_retry(monkeypatch):
    """Zero the framework backoff base so retry tests don't sleep for real."""
    import metagpt.executor.tasks.bggraph.engine as eng

    monkeypatch.setattr(eng, "_RETRY_WAIT", 0.0)
    return eng


class TestAutoRetries:
    """Retry budget is owned by the engine (``_AUTO_RETRIES``), not the node."""

    async def test_retry_then_succeed(self, fast_retry):
        counter: list = []
        g = BgGraph("retry", state_schema=S)
        # Fails twice (within the framework's 3-retry budget), then succeeds.
        g.add_node("a", flaky_node(2, "ok", counter))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        assert await _run(g, x=0) == "ok"
        assert len(counter) == 3  # 2 failures + 1 success

    async def test_retry_exhausted_fails(self, fast_retry):
        counter: list = []
        g = BgGraph("retryfail", state_schema=S)
        # Always fails → exhausts the framework budget (_AUTO_RETRIES).
        g.add_node("a", flaky_node(99, "never", counter))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        with pytest.raises(GraphBatchFailureError) as ei:
            await _run(g, x=0)
        name, exc = ei.value.failures[0]
        assert name == "a"
        # Recorded retry counts reflect the framework budget.
        assert getattr(exc, "_auto_retries_attempted", None) == fast_retry._AUTO_RETRIES
        assert getattr(exc, "_auto_retries_limit", None) == fast_retry._AUTO_RETRIES
        assert len(counter) == fast_retry._AUTO_RETRIES + 1  # initial + budget retries

    async def test_non_retryable_error_fails_immediately(self, fast_retry):
        """A non-retryable error (ValueError) is never retried, regardless of budget."""
        counter: list = []
        g = BgGraph("noretry", state_schema=S)
        g.add_node("a", non_retryable_flaky_node(5, "never", counter))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        with pytest.raises(GraphBatchFailureError) as ei:
            await _run(g, x=0)
        name, exc = ei.value.failures[0]
        assert name == "a"
        assert isinstance(exc, ValueError)
        # Only 1 attempt — no retries for non-retryable errors.
        assert len(counter) == 1
        assert getattr(exc, "_auto_retries_attempted", None) == 0
        assert getattr(exc, "_auto_retries_limit", None) == fast_retry._AUTO_RETRIES


class TestRetryBackoff:
    """Engine-owned exponential backoff with full jitter, capped at the ceiling."""

    async def test_exponential_ceiling_grows(self, monkeypatch):
        import metagpt.executor.tasks.bggraph.engine as eng

        # Pin the base and jitter to its upper bound to assert the exponential ceiling.
        monkeypatch.setattr(eng, "_RETRY_WAIT", 1.0)
        monkeypatch.setattr(eng.random, "uniform", lambda lo, hi: hi)
        assert eng._retry_delay(1) == 1.0  # 1 * 2**0
        assert eng._retry_delay(2) == 2.0  # 1 * 2**1
        assert eng._retry_delay(3) == 4.0  # 1 * 2**2

    async def test_capped_at_max(self, monkeypatch):
        import metagpt.executor.tasks.bggraph.engine as eng

        monkeypatch.setattr(eng, "_RETRY_WAIT", 5.0)
        monkeypatch.setattr(eng.random, "uniform", lambda lo, hi: hi)
        # A huge attempt count is clamped to the 60s ceiling.
        assert eng._retry_delay(20) == eng._MAX_BACKOFF_WAIT

    async def test_jitter_within_bounds(self, monkeypatch):
        import metagpt.executor.tasks.bggraph.engine as eng

        monkeypatch.setattr(eng, "_RETRY_WAIT", 2.0)
        # With real jitter every sample stays within [0, capped_ceiling].
        for attempt in range(1, 6):
            ceiling = min(2.0 * (2 ** (attempt - 1)), eng._MAX_BACKOFF_WAIT)
            for _ in range(20):
                d = eng._retry_delay(attempt)
                assert 0.0 <= d <= ceiling

    async def test_backoff_delays_recorded_during_run(self, monkeypatch):
        # Capture the actual sleeps a node incurs across its retries.
        import metagpt.executor.tasks.bggraph.engine as eng

        slept: list = []

        async def fake_sleep(d):
            slept.append(d)

        monkeypatch.setattr(eng, "_RETRY_WAIT", 1.0)
        monkeypatch.setattr(eng.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(eng.random, "uniform", lambda lo, hi: hi)

        counter: list = []
        g = BgGraph("backoff", state_schema=S)
        g.add_node("a", flaky_node(3, "ok", counter))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        assert await _run(g, x=0) == "ok"
        # 3 failures + 1 success → 3 retries → 3 sleeps, exponentially growing.
        assert slept == [1.0, 2.0, 4.0]


class TestLlmPause:
    async def test_pause_returns_llm_pause_result(self):
        g = BgGraph("llm", state_schema=S)
        g.add_node("a", sync_node(lambda s: "a-done"))
        g.add_node("nextstep", sync_node(lambda s: "next"))
        g.add_edge(START, "a")
        g.add_llm_edges("a", "Pick next", {"go": "nextstep", "stop": END})
        g.add_edge("nextstep", END)
        res = await g.compile()(x=0)
        out = await res.poll
        # isinstance check works with the backward-compat alias
        assert isinstance(out, _LLM_ROUTE_SENTINEL)
        assert isinstance(out, LlmPauseResult)
        # Carries pause state
        assert "a" in out.completed
        assert out.state is not None
        assert getattr(out.state, "a") == "a-done"
        assert out.edge.from_node == "a"
        assert "go" in out.edge.mapping
