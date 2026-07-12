#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``ReActLoop.run`` — the full think→act orchestration.

Covers: the no-news short-circuit, the ``set_active(True)`` gate, the terminal
(native plain-text) finish path, a single act-then-stop, the deactivate→break
path (End tool), the background-pool wait branch, and the two post-checks
(max_react_loop cap + consecutive-react limit, each with/without AskUserQuestion).
"""
from __future__ import annotations

import pytest

from metagpt.common.schema import CauseBy, UserMessage

from .conftest import FakeChannel, FakeExecutor, FakeResult, FakeThinkEngine, FakeBgPool

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


class _SeqAskExecutor(FakeExecutor):
    """AskUserQuestion returns a scripted sequence of replies (then repeats the last)."""

    def __init__(self, ask_replies, **kw):
        super().__init__(**kw)
        self._ask_replies = list(ask_replies)
        self.ask_outputs: list[str] = []

    async def run_command(self, name, args, result_id=None):
        if name == "AskUserQuestion":
            self.calls.append({"name": name, "args": args, "result_id": result_id})
            reply = self._ask_replies.pop(0) if self._ask_replies else self.ask_outputs[-1]
            self.ask_outputs.append(reply)
            return FakeResult(output=reply)
        return await super().run_command(name, args, result_id)


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
    # max_react_loop=1 -> exactly one act, then the while-condition exits.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "Read", "args": {}}])
    executor = FakeExecutor(results={"Read": FakeResult(output="data")})
    b = make_loop(channel=channel, executor=executor, max_react_loop=1, max_consecutive_react_limit=99)
    _news(b)

    rsp = await b.loop.run()

    assert [c["name"] for c in executor.calls] == ["Read"]
    assert "data" in rsp.content


async def test_run_deactivate_breaks_loop(make_loop):
    # An End-like command deactivates mid-act; the next think returns False and,
    # with no pending background work, the loop breaks.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "End", "args": {}}])
    executor = _DeactExecutor("End")
    b = make_loop(channel=channel, executor=executor, max_react_loop=9, max_consecutive_react_limit=99)
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
    b = make_loop(
        channel=channel, executor=executor, bg_pool=bg, max_react_loop=9, max_consecutive_react_limit=99
    )
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
        max_react_loop=9,
        max_consecutive_react_limit=99,
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
    b = make_loop(channel=channel, executor=executor, max_react_loop=9)
    _news(b)

    await b.loop.run()

    assert executor.calls == []
    assert channel.is_terminal_calls == 1


async def test_run_consecutive_limit_asks_human(make_loop):
    # No new observations between acts -> consecutive climbs to the limit and,
    # since AskUserQuestion is available, the loop asks the user (via the routed
    # LLM) and records the extra instruction, resetting the consecutive counter.
    channel = FakeChannel(commands=[])  # empty-command acts keep it simple
    executor = _SeqAskExecutor(["keep going"])
    b = make_loop(
        channel=channel,
        executor=executor,
        tools=["Read", "AskUserQuestion"],
        max_react_loop=5,
        max_consecutive_react_limit=2,
    )
    _news(b)

    await b.loop.run()

    ask_calls = [c for c in executor.calls if c["name"] == "AskUserQuestion"]
    assert len(ask_calls) >= 1
    # The routed LLM produced the question handed to AskUserQuestion.
    assert b.provider.llm.aask_calls
    assert ask_calls[0]["args"]["questions"][0]["question"] == b.provider.llm.reply
    # The user's reply was committed back into memory.
    assert any("User's extra instruction:" in m.content for m in b.memory.messages)


async def test_run_consecutive_limit_breaks_without_ask_human(make_loop):
    # Same climb, but no AskUserQuestion capability -> the loop simply breaks.
    channel = FakeChannel(commands=[])
    executor = FakeExecutor()
    b = make_loop(
        channel=channel,
        executor=executor,
        tools=["Read"],  # no AskUserQuestion
        max_react_loop=9,
        max_consecutive_react_limit=2,
    )
    _news(b)

    await b.loop.run()

    assert [c for c in executor.calls if c["name"] == "AskUserQuestion"] == []
    # Broke after exactly two acts (consecutive hit the limit).
    assert len(channel.recorded_turns) == 2


async def test_run_max_loop_reached_yes_resets(make_loop):
    # max_react_loop>=10 arms the cap check. AskUserQuestion says "yes" once
    # (reset to 0, continue) then "no" (fall through, while-condition exits).
    channel = FakeChannel(commands=[])
    executor = _SeqAskExecutor(["yes", "no"])
    b = make_loop(
        channel=channel,
        executor=executor,
        tools=["Read", "AskUserQuestion"],
        max_react_loop=10,
        max_consecutive_react_limit=10_000,  # keep the consecutive branch silent
    )
    _news(b)

    rsp = await b.loop.run()

    asks = [c for c in executor.calls if c["name"] == "AskUserQuestion"]
    assert len(asks) == 2
    assert executor.ask_outputs == ["yes", "no"]
    assert rsp is not None


async def test_run_max_loop_reached_breaks_without_ask_human(make_loop):
    channel = FakeChannel(commands=[])
    executor = FakeExecutor()
    b = make_loop(
        channel=channel,
        executor=executor,
        tools=["Read"],  # no AskUserQuestion
        max_react_loop=10,
        max_consecutive_react_limit=10_000,
    )
    _news(b)

    await b.loop.run()

    assert [c for c in executor.calls if c["name"] == "AskUserQuestion"] == []
    # Ran exactly up to the cap, then broke.
    assert len(channel.recorded_turns) == 10
