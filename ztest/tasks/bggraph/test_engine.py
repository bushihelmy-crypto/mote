#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the bggraph frontier scheduler (:mod:`mote.executor.bggraph.engine`).

Covers linear chains, parallel fan-out, waiting-edge AND-joins (fast + slow
source), conditional routing, cycles bounded by ``recursion_limit``, independent
parallel failure, auto-retries, and the LLM-route pause sentinel.

``report_progress`` is a no-op outside a progress context, so the driver coroutine
(``BgTaskResult.poll_factory()``) can be awaited directly without a pool / disk sink.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.executor.tasks.bggraph import END, START, BgGraph, GraphBatchFailureError, GraphRecursionError
from mote.executor.tasks.bggraph.types import _LLM_ROUTE_SENTINEL, LlmPauseResult
from mote.executor.tasks.types import BgTaskResult

from .conftest import S, boom_node, flaky_node, gated_node, non_retryable_flaky_node, sync_node

pytestmark = pytest.mark.asyncio


async def _run(graph: BgGraph, **inputs):
    res = await graph.compile()(**inputs)
    assert isinstance(res, BgTaskResult)
    return await res.poll_factory()


class TestLinear:
    async def test_single_node(self):
        g = BgGraph("lin1", state_schema=S)
        g.add_node("a", sync_node(lambda s: s.x + 1, field="a"))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        assert (await _run(g, x=41))["a"] == 42

    async def test_chain(self):
        g = BgGraph("chain", state_schema=S)
        g.add_node("a", sync_node(lambda s: s.x + 1, field="a"))
        g.add_node("b", sync_node(lambda s: s.a * 2, field="b"))
        g.add_node("c", sync_node(lambda s: s.b + 10, field="c"))
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", END)
        # x=0 → a=1 → b=2 → c=12
        assert (await _run(g, x=0))["c"] == 12


class TestFanOut:
    async def test_parallel_then_join(self):
        g = BgGraph("fan", state_schema=S)
        g.add_node("a", sync_node(lambda s: s.x + 1, field="a"))
        g.add_node("tts", sync_node(lambda s: s.a * 10, field="tts"))
        g.add_node("render", sync_node(lambda s: s.a * 100, field="render"))
        g.add_node("merge", sync_node(lambda s: {"v": s.tts + s.render}, field="merge"))
        g.add_edge(START, "a")
        g.add_edge("a", "tts")
        g.add_edge("a", "render")
        g.add_edge(["tts", "render"], "merge")
        g.add_edge("merge", END)
        # x=0 → a=1 → tts=10, render=100 → merge=110
        assert (await _run(g, x=0))["merge"] == {"v": 110}

    async def test_waiting_edge_fast_and_slow_source(self):
        """AND-join must wait for the *slow* source before firing the merge."""
        slow = asyncio.Event()
        g = BgGraph("waitslow", state_schema=S)
        g.add_node("a", sync_node(lambda s: 1, field="a"))
        g.add_node("fast", sync_node(lambda s: "fast", field="fast"))
        g.add_node("slow", gated_node(slow, lambda s: "slow", field="slow"))
        g.add_node("merge", sync_node(lambda s: [s.fast, s.slow], field="merge"))
        g.add_edge(START, "a")
        g.add_edge("a", "fast")
        g.add_edge("a", "slow")
        g.add_edge(["fast", "slow"], "merge")
        g.add_edge("merge", END)

        res = await g.compile()(x=0)
        task = asyncio.ensure_future(res.poll_factory())
        # Let the fast source settle; merge must NOT have fired yet.
        await asyncio.sleep(0.02)
        assert not task.done()
        slow.set()
        assert (await task)["merge"] == ["fast", "slow"]


class TestWaitingEdgeMerge:
    async def test_single_edge_folded_fires_merge_once(self):
        """A single edge + waiting edge into the same target is folded into one
        AND-join over the union; the merge fires exactly once after ALL sources.

        Without the fold (the old double-channel bug) ``merge`` would fire twice:
        once on the single edge's source and again on the join.
        """
        fires: list[int] = []
        slow = asyncio.Event()

        def merge_fn(s):
            fires.append(1)
            return {"a": s.a, "b": s.b, "c": s.c}

        g = BgGraph("foldmerge", state_schema=S)
        # 'a' is the slow single-edge source; b/c are the join sources.
        g.add_node("a", gated_node(slow, lambda s: "A", field="a"))
        g.add_node("b", sync_node(lambda s: "B", field="b"))
        g.add_node("c", sync_node(lambda s: "C", field="c"))
        g.add_node("merge", sync_node(merge_fn, field="merge"))
        g.add_edge(START, "seed")
        g.add_node("seed", sync_node(lambda s: 0, field="seed"))
        g.add_edge("seed", "a")
        g.add_edge("seed", "b")
        g.add_edge("seed", "c")
        g.add_edge("a", "merge")  # folded into the join → merge ← [b, c, a]
        g.add_edge(["b", "c"], "merge")
        g.add_edge("merge", END)

        res = await g.compile()(x=0)
        task = asyncio.ensure_future(res.poll_factory())
        # b and c land fast; merge must still wait for the slow 'a'.
        await asyncio.sleep(0.02)
        assert fires == []
        assert not task.done()
        slow.set()
        out = await task
        assert out["merge"] == {"a": "A", "b": "B", "c": "C"}
        assert fires == [1]  # fired exactly once


class TestConditional:
    async def test_routes_on_post_state(self):
        g = BgGraph("cond", state_schema=S)
        g.add_node("a", sync_node(lambda s: s.x, field="a"))
        g.add_node("big", sync_node(lambda s: "BIG", field="big"))
        g.add_node("small", sync_node(lambda s: "SMALL", field="small"))
        g.add_edge(START, "a")
        g.add_conditional_edges("a", lambda s: "big" if s.a > 10 else "small", {"big": "big", "small": "small"})
        g.add_edge("big", END)
        g.add_edge("small", END)
        assert (await _run(g, x=20))["big"] == "BIG"
        assert (await _run(g, x=3))["small"] == "SMALL"


class TestCycle:
    async def test_recursion_limit_exceeded(self):
        g = BgGraph("cyc", state_schema=S, recursion_limit=5)
        g.add_node("a", sync_node(lambda s: (getattr(s, "b", None) or 0) + 1, field="a"))
        g.add_node("b", sync_node(lambda s: (s.a or 0) + 1, field="b"))
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_conditional_edges("b", lambda s: "loop", {"loop": "a", "done": END})
        with pytest.raises(GraphRecursionError):
            await _run(g, x=0)

    async def test_bounded_cycle_completes(self):
        """A cycle that exits before the limit returns normally."""
        g = BgGraph("cyc2", state_schema=S, recursion_limit=50)
        g.add_node("a", sync_node(lambda s: (getattr(s, "a", None) or 0) + 1, field="a"))
        g.add_edge(START, "a")
        # Loop back to 'a' until its result reaches 3, then go to END.
        g.add_conditional_edges("a", lambda s: "done" if s.a >= 3 else "loop", {"loop": "a", "done": END})
        assert (await _run(g, x=0))["a"] == 3

    async def test_and_join_inside_cycle_re_waits_each_lap(self):
        """An AND-join inside a cycle must re-collect ALL sources every lap.

        The merge fans back to both sources; the *slow* source blocks on a
        per-lap queue token while *fast* is synchronous. Without resetting the
        arrival set on fire, the set stays full after lap 1, so on lap 2 the
        merge re-fires the instant fast lands — observing a STALE slow value
        from lap 1 (OR-join degradation). With per-activation reset the merge
        must wait for the slow source again, so every invocation sees fast and
        slow advanced in lockstep.
        """
        from mote.executor.tasks.bggraph import Stage

        # One token must be released per slow-source invocation (one per lap).
        tokens: asyncio.Queue = asyncio.Queue()
        # Every (fast, slow) pair the merge node observes when it runs.
        observed: list[tuple] = []

        async def slow_node(state):
            async def submit():
                await tokens.get()
                return {"slow": (getattr(state, "slow", None) or 0) + 1}

            return Stage(submit=submit())

        def merge_fn(s):
            observed.append((s.fast, s.slow))
            return {"f": s.fast, "sl": s.slow}

        g = BgGraph("andcyc", state_schema=S, recursion_limit=50)
        g.add_node("a", sync_node(lambda s: s.x, field="a"))
        g.add_node("fast", sync_node(lambda s: (getattr(s, "fast", None) or 0) + 1, field="fast"))
        g.add_node("slow", slow_node)
        g.add_node("merge", sync_node(merge_fn, field="merge"))

        g.add_edge(START, "a")
        g.add_edge("a", "fast")
        g.add_edge("a", "slow")
        g.add_edge(["fast", "slow"], "merge")
        # Loop once (back to the fan-out source), then end on the second merge.
        g.add_conditional_edges(
            "merge",
            lambda s: "loop" if s.fast < 2 else "done",
            {"loop": "a", "done": END},
        )

        res = await g.compile()(x=0)
        task = asyncio.ensure_future(res.poll_factory())

        # Lap 1: fast lands immediately; merge must wait for the slow token.
        await asyncio.sleep(0.02)
        assert observed == []  # AND-join has not fired without slow
        await tokens.put(None)  # release slow lap 1 → merge (1, 1) → loops back

        # Lap 2: fast lands again. A correct AND-join must NOT fire yet — the
        # arrival set was reset on the lap-1 fire, so it is waiting on slow lap 2.
        await asyncio.sleep(0.02)
        # The bug would have fired merge here observing the stale (2, 1).
        assert observed == [(1, 1)]
        await tokens.put(None)  # release slow lap 2 → final merge (2, 2) → END

        out = await task
        assert out["merge"] == {"f": 2, "sl": 2}
        # Every merge saw the two sources in lockstep — never a stale lap.
        assert observed == [(1, 1), (2, 2)]


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
        g.add_node("a", sync_node(lambda s: 1, field="a"))
        g.add_node("bad", boom_node(ValueError("bad branch")))
        g.add_node("good", sync_node(lambda s: "good-done", field="good"))
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
    import mote.executor.tasks.bggraph.engine as eng

    monkeypatch.setattr(eng, "_RETRY_WAIT", 0.0)
    return eng


class TestAutoRetries:
    """Retry budget is owned by the engine (``_AUTO_RETRIES``), not the node."""

    async def test_retry_then_succeed(self, fast_retry):
        counter: list = []
        g = BgGraph("retry", state_schema=S)
        # Fails twice (within the framework's 3-retry budget), then succeeds.
        g.add_node("a", flaky_node(2, "ok", counter, field="a"))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        assert (await _run(g, x=0))["a"] == "ok"
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
        # Recorded retry counts reflect the framework budget — read from the
        # authoritative run-state record, not a monkey-patched exception attr.
        rec = ei.value.run_state.records["a"]
        assert rec.retries_attempted == fast_retry._AUTO_RETRIES
        assert rec.retries_limit == fast_retry._AUTO_RETRIES
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
        rec = ei.value.run_state.records["a"]
        assert rec.retries_attempted == 0
        assert rec.retries_limit == fast_retry._AUTO_RETRIES


class TestRetryBackoff:
    """Engine-owned exponential backoff with full jitter, capped at the ceiling."""

    async def test_exponential_ceiling_grows(self, monkeypatch):
        import mote.executor.tasks.bggraph.engine as eng

        # Pin the base and jitter to its upper bound to assert the exponential ceiling.
        monkeypatch.setattr(eng, "_RETRY_WAIT", 1.0)
        monkeypatch.setattr(eng.random, "uniform", lambda lo, hi: hi)
        assert eng._retry_delay(1) == 1.0  # 1 * 2**0
        assert eng._retry_delay(2) == 2.0  # 1 * 2**1
        assert eng._retry_delay(3) == 4.0  # 1 * 2**2

    async def test_capped_at_max(self, monkeypatch):
        import mote.executor.tasks.bggraph.engine as eng

        monkeypatch.setattr(eng, "_RETRY_WAIT", 5.0)
        monkeypatch.setattr(eng.random, "uniform", lambda lo, hi: hi)
        # A huge attempt count is clamped to the 60s ceiling.
        assert eng._retry_delay(20) == eng._MAX_BACKOFF_WAIT

    async def test_jitter_within_bounds(self, monkeypatch):
        import mote.executor.tasks.bggraph.engine as eng

        monkeypatch.setattr(eng, "_RETRY_WAIT", 2.0)
        # With real jitter every sample stays within [0, capped_ceiling].
        for attempt in range(1, 6):
            ceiling = min(2.0 * (2 ** (attempt - 1)), eng._MAX_BACKOFF_WAIT)
            for _ in range(20):
                d = eng._retry_delay(attempt)
                assert 0.0 <= d <= ceiling

    async def test_backoff_delays_recorded_during_run(self, monkeypatch):
        # Capture the actual sleeps a node incurs across its retries.
        import mote.executor.tasks.bggraph.engine as eng

        slept: list = []

        async def fake_sleep(d):
            slept.append(d)

        monkeypatch.setattr(eng, "_RETRY_WAIT", 1.0)
        monkeypatch.setattr(eng.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(eng.random, "uniform", lambda lo, hi: hi)

        counter: list = []
        g = BgGraph("backoff", state_schema=S)
        g.add_node("a", flaky_node(3, "ok", counter, field="a"))
        g.add_edge(START, "a")
        g.add_edge("a", END)
        assert (await _run(g, x=0))["a"] == "ok"
        # 3 failures + 1 success → 3 retries → 3 sleeps, exponentially growing.
        assert slept == [1.0, 2.0, 4.0]


class TestLlmPause:
    async def test_pause_returns_llm_pause_result(self):
        g = BgGraph("llm", state_schema=S)
        g.add_node("a", sync_node(lambda s: "a-done", field="a"))
        g.add_node("nextstep", sync_node(lambda s: "next", field="nextstep"))
        g.add_edge(START, "a")
        g.add_llm_edges("a", "Pick next", {"go": "nextstep", "stop": END})
        g.add_edge("nextstep", END)
        res = await g.compile()(x=0)
        out = await res.poll_factory()
        # isinstance check works with the backward-compat alias
        assert isinstance(out, _LLM_ROUTE_SENTINEL)
        assert isinstance(out, LlmPauseResult)
        # Carries pause state
        assert "a" in out.completed
        assert out.state is not None
        assert getattr(out.state, "a") == "a-done"
        assert out.edge.from_node == "a"
        assert "go" in out.edge.mapping
