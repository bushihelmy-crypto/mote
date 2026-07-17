#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``ReActLoop.run`` — the full think→act orchestration.

Covers: the no-news short-circuit, the ``set_active(True)`` gate, the terminal
(native plain-text) finish path, a single act-then-stop (terminate result), the
deactivate→break path (End tool), the background-pool wait branch, and the
budget gate (stop vs proceed). The loop has no iteration cap of its own — it
terminates purely on those natural exits.
"""
from __future__ import annotations

import pytest

from mote.common.base import BudgetVerdict
from mote.common.schema import CauseBy, UserMessage

from .conftest import FakeBgPool, FakeChannel, FakeExecutor, FakeResult, FakeThinkEngine

pytestmark = pytest.mark.asyncio


def _news(b, *, name="Alice"):
    """Push a single message addressed to the role so the initial observe fires."""
    b.buffer.push(UserMessage("go", send_to={name}))


class _DeactExecutor(FakeExecutor):
    """Executor that flips the shared active holder off after a named command."""

    def __init__(self, deact_on, **kw):
        super().__init__(**kw)
        self.deact_on = deact_on
        self.holder = None  # wired to the loop's active holder after build

    async def run_command(self, name, args, result_id=None):
        r = await super().run_command(name, args, result_id)
        if name == self.deact_on and self.holder is not None:
            self.holder[0] = False
        return r


class _SeqTerminalChannel(FakeChannel):
    """Channel whose ``is_terminal`` walks a scripted sequence.

    Models a native run that acts a few rounds and then finishes: each entry of
    ``terminal_seq`` answers one ``is_terminal`` check; once exhausted it reports
    terminal (True) so a runaway loop still stops. ``is_terminal_calls`` counts
    how many times the loop consulted it.
    """

    def __init__(self, terminal_seq, **kw):
        super().__init__(**kw)
        self._seq = list(terminal_seq)
        self.is_terminal_calls = 0

    async def is_terminal(self, think_engine) -> bool:
        self.is_terminal_calls += 1
        return self._seq.pop(0) if self._seq else True


async def test_run_returns_none_without_news(make_loop):
    b = make_loop()  # buffer empty
    rsp = await b.loop.run()
    assert rsp is None
    # Never thought, never activated past its initial value.
    assert b.think_engine.start_calls == []


async def test_run_activates_even_if_starting_inactive(make_loop):
    # active starts False; run() must set it True after observing news so the
    # first think proceeds. Terminal channel -> _finish path.
    engine = FakeThinkEngine(content="final answer")
    channel = FakeChannel(terminal=True)
    b = make_loop(active=False, think_engine=engine, channel=channel)
    _news(b)

    rsp = await b.loop.run()

    assert b.active[0] is True
    assert rsp.content == "final answer"
    assert rsp.cause_by == CauseBy.RUN_COMMAND.value
    # _finish records an empty-command turn and joins.
    assert channel.recorded_turns == [("final answer", [])]
    assert b.think_engine.join_calls == 1


async def test_run_terminal_skips_act(make_loop):
    engine = FakeThinkEngine(content="done")
    channel = FakeChannel(terminal=True)
    executor = FakeExecutor()
    b = make_loop(think_engine=engine, channel=channel, executor=executor)
    _news(b)

    await b.loop.run()

    # Terminal turn -> no commands executed.
    assert executor.calls == []


async def test_run_single_act_then_stop(make_loop):
    # A terminate=True result flips active off after one act; the next think then
    # returns False and, with no pending background work, the loop breaks.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "Read", "args": {}}])
    executor = FakeExecutor(results={"Read": FakeResult(output="data", terminate=True)})
    b = make_loop(channel=channel, executor=executor)
    _news(b)

    rsp = await b.loop.run()

    assert [c["name"] for c in executor.calls] == ["Read"]
    assert "data" in rsp.content


async def test_run_deactivate_breaks_loop(make_loop):
    # An End-like command deactivates mid-act; the next think returns False and,
    # with no pending background work, the loop breaks.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "End", "args": {}}])
    executor = _DeactExecutor("End")
    b = make_loop(channel=channel, executor=executor)
    executor.holder = b.active
    _news(b)

    rsp = await b.loop.run()

    assert b.active[0] is False
    # Only one act ran (End), then the loop broke on the inactive think.
    assert len([c for c in executor.calls if c["name"] == "End"]) == 1
    assert rsp.cause_by == CauseBy.RUN_COMMAND.value


async def test_run_waits_on_pending_background_tasks(make_loop):
    # When think yields nothing but the bg pool is busy, the loop parks on
    # wait_any() instead of breaking, then re-observes and continues.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "End", "args": {}}])
    executor = _DeactExecutor("End")
    bg = FakeBgPool(pending=1)
    b = make_loop(channel=channel, executor=executor, bg_pool=bg)
    executor.holder = b.active
    _news(b)

    await b.loop.run()

    assert bg.wait_any_calls >= 1
    assert bg.pending == 0  # drained


async def test_run_acts_several_rounds_then_finishes(make_loop):
    # Native flow: the channel reports non-terminal for two rounds (so act runs
    # twice) and terminal on the third, so the loop finishes on that turn. The
    # is_terminal check is consulted once per think round, *before* act.
    engine = FakeThinkEngine(content="final text")
    channel = _SeqTerminalChannel(
        commands=[{"id": "t1", "command_name": "Read", "args": {}}],
        terminal_seq=[False, False, True],
    )
    executor = FakeExecutor(results={"Read": FakeResult(output="data")})
    b = make_loop(
        think_engine=engine,
        channel=channel,
        executor=executor,
    )
    _news(b)

    rsp = await b.loop.run()

    # Two acts ran (one Read each), then the terminal round finished.
    assert [c["name"] for c in executor.calls] == ["Read", "Read"]
    assert channel.is_terminal_calls == 3
    # Recorded turns: two act turns (with the command) + one finish turn (empty).
    assert len(channel.recorded_turns) == 3
    assert channel.recorded_turns[-1] == ("final text", [])
    # The finish path surfaces the assistant's plain text as the response.
    assert rsp.content == "final text"
    assert rsp.cause_by == CauseBy.RUN_COMMAND.value


async def test_run_terminal_checked_before_act_each_round(make_loop):
    # A terminal verdict on the very first round skips act entirely, even though
    # the channel has commands queued — the loop checks is_terminal first.
    channel = _SeqTerminalChannel(
        commands=[{"id": "t1", "command_name": "Read", "args": {}}],
        terminal_seq=[True],
    )
    executor = FakeExecutor()
    b = make_loop(channel=channel, executor=executor)
    _news(b)

    await b.loop.run()

    assert executor.calls == []
    assert channel.is_terminal_calls == 1


async def test_run_budget_stop_halts_before_think(make_loop):
    # A hard-cap verdict must break the loop *before* any think: the engine is
    # never started and no command runs. The verdict message becomes the reply.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "Read", "args": {}}])
    executor = FakeExecutor(results={"Read": FakeResult(output="data")})
    b = make_loop(channel=channel, executor=executor)
    b.provider.budget_verdict = BudgetVerdict(stop=True, message="budget-halt")
    _news(b)

    rsp = await b.loop.run()

    assert b.provider.enforce_budget_calls == 1
    assert b.provider.prepare_calls == 0  # never assembled a think request
    assert b.think_engine.start_calls == []  # never touched the LLM
    assert executor.calls == []  # never acted
    assert rsp.content == "budget-halt"
    assert rsp.cause_by == CauseBy.RUN_COMMAND.value


async def test_run_budget_proceed_allows_normal_act(make_loop):
    # The default PROCEED verdict is transparent: the loop thinks + acts as usual
    # and consults the gate on the turn it ran. A terminate result stops it after
    # one act so the test is bounded.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "Read", "args": {}}])
    executor = FakeExecutor(results={"Read": FakeResult(output="data", terminate=True)})
    b = make_loop(channel=channel, executor=executor)
    _news(b)

    rsp = await b.loop.run()

    # Gate consulted every turn: once before the act, once on the post-terminate
    # think that finds nothing to do and breaks.
    assert b.provider.enforce_budget_calls >= 1
    assert [c["name"] for c in executor.calls] == ["Read"]
    assert "data" in rsp.content
