#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``AgentFlowEngine.run`` — the full graph orchestration.

Covers: the no-news short-circuit, the ``set_active(True)`` gate, the terminal
(native plain-text) finish path, a single act-then-stop (terminate result), the
deactivate→break path (End tool), the background-pool wait branch, and the
budget gate (stop vs proceed). The loop has no iteration cap of its own — it
terminates purely on those natural exits.
"""
from __future__ import annotations

import pytest

from mote.contracts.schema import CauseBy, UserMessage
from mote.kernel.flow import BudgetVerdict

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
    """Channel whose semantic final-candidate signal walks a sequence.

    Models a native run that acts a few rounds and then finishes: each entry of
    ``terminal_seq`` answers one ``is_terminal`` check; once exhausted it reports
    terminal (True) so a runaway loop still stops. ``is_terminal_calls`` counts
    how many times the loop consulted it.
    """

    def __init__(self, terminal_seq, **kw):
        super().__init__(**kw)
        self._seq = list(terminal_seq)
        self.is_terminal_calls = 0

    async def model_turn(self, think_engine):
        from mote.contracts.model_actions import FinalCandidateAction, ModelTurn

        self.is_terminal_calls += 1
        terminal = self._seq.pop(0) if self._seq else True
        content = think_engine.result.content or ""
        if terminal:
            return ModelTurn(
                content=content,
                actions=[FinalCandidateAction(raw=content, representation="test")],
            )
        return await super().model_turn(think_engine)


async def test_run_returns_none_without_news(make_engine):
    b = make_engine()  # buffer empty
    rsp = await b.engine.run()
    assert rsp is None


async def test_resume_accepted_output_commits_without_news_or_model_call(make_engine):
    from mote.kernel.output import text_output_contract
    from mote.runtime.agent.output_engine import OutputEngine

    contract = text_output_contract()
    engine = OutputEngine(
        contract,
        restored_state={
            "status": "commit_started",
            "candidate_id": "candidate-1",
            "contract_id": "mote.text@1",
            "schema_fingerprint": contract.decoder.schema.fingerprint,
            "value": "recovered",
            "correction_attempts": 0,
        },
    )
    think = FakeThinkEngine(content="must not run")
    b = make_engine(think_engine=think, output_engine=engine)

    result = await b.engine.run()

    assert result is not None
    assert result.committed_output is not None
    assert result.committed_output.value == "recovered"
    assert result.presentation.content == "recovered"
    assert think.start_calls == []
    # Never thought, never activated past its initial value.
    assert b.think_engine.start_calls == []


async def test_run_activates_even_if_starting_inactive(make_engine):
    # active starts False; run() must set it True after observing news so the
    # first think proceeds. Terminal channel -> _finish path.
    engine = FakeThinkEngine(content="final answer")
    channel = FakeChannel(terminal=True)
    b = make_engine(active=False, think_engine=engine, channel=channel)
    _news(b)

    rsp = await b.engine.run()

    assert b.active[0] is True
    assert rsp.presentation.content == "final answer"
    assert rsp.presentation.cause_by == CauseBy.RUN_COMMAND.value
    # _finish records an empty-command turn and joins.
    assert channel.recorded_turns == [("final answer", [])]
    assert b.think_engine.join_calls == 1


async def test_run_terminal_skips_act(make_engine):
    engine = FakeThinkEngine(content="done")
    channel = FakeChannel(terminal=True)
    executor = FakeExecutor()
    b = make_engine(think_engine=engine, channel=channel, executor=executor)
    _news(b)

    await b.engine.run()

    # Terminal turn -> no commands executed.
    assert executor.calls == []


async def test_rejected_output_records_feedback_then_accepts_next_candidate(make_engine):
    from mote.contracts.output import OutputEvaluation, ValidationIssue

    class RejectOnce:
        run_id = "reject-once-run"

        def __init__(self):
            self.calls = 0

        @property
        def has_restored_terminal_output(self):
            return False

        async def evaluate(self, candidate):
            self.calls += 1
            if self.calls == 1:
                return OutputEvaluation(
                    accepted=False,
                    correction_allowed=True,
                    issues=(ValidationIssue(("count",), "int_parsing", "Expected an integer"),),
                )
            return OutputEvaluation(accepted=True, value=candidate.raw)

        async def commit(self):
            from mote.contracts.output import CommittedOutput

            return CommittedOutput("fake", "test.output@1", "sha", None)

    engine = RejectOnce()
    channel = FakeChannel(terminal=True)
    b = make_engine(
        think_engine=FakeThinkEngine(content="candidate"),
        channel=channel,
        output_engine=engine,
    )
    _news(b)

    rsp = await b.engine.run()

    assert rsp.presentation.content == "candidate"
    assert engine.calls == 2
    assert len(channel.output_feedback) == 1
    assert channel.output_feedback[0].issues[0].path == ("count",)


async def test_output_correction_budget_bounds_model_turns(make_engine):
    from pydantic import BaseModel

    from mote.contracts.output import OutputContractId
    from mote.kernel.output import OutputContract, OutputRetryPolicy, TypeAdapterOutputDecoder
    from mote.runtime.agent.output_engine import OutputEngine
    from mote.runtime.errors import OutputCorrectionExhaustedError

    class Report(BaseModel):
        count: int

    engine = OutputEngine(
        OutputContract(
            OutputContractId("test", "report", "1"),
            TypeAdapterOutputDecoder(Report),
            OutputRetryPolicy(max_corrections=2),
        )
    )
    channel = FakeChannel(terminal=True)
    b = make_engine(
        think_engine=FakeThinkEngine(content="still invalid"),
        channel=channel,
        output_engine=engine,
    )
    _news(b)

    with pytest.raises(OutputCorrectionExhaustedError) as caught:
        await b.engine.run()

    assert len(b.think_engine.start_calls) == 3
    assert engine.correction_attempts == 2
    assert len(channel.output_feedback) == 2
    assert caught.value.code.value == "OUTPUT_CORRECTION_EXHAUSTED"
    assert caught.value.retryable is False


async def test_run_single_act_then_stop(make_engine):
    # A terminate=True result flips active off after one act; the next think then
    # returns False and, with no pending background work, the loop breaks.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "Read", "args": {}}])
    executor = FakeExecutor(results={"Read": FakeResult(output="data", terminate=True)})
    b = make_engine(channel=channel, executor=executor)
    _news(b)

    rsp = await b.engine.run()

    assert [c["name"] for c in executor.calls] == ["Read"]
    assert "data" in rsp.presentation.content


async def test_run_deactivate_breaks_loop(make_engine):
    # An End-like command deactivates mid-act; the next think returns False and,
    # with no pending background work, the loop breaks.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "End", "args": {}}])
    executor = _DeactExecutor("End")
    b = make_engine(channel=channel, executor=executor)
    executor.holder = b.active
    _news(b)

    rsp = await b.engine.run()

    assert b.active[0] is False
    # Only one act ran (End), then the loop broke on the inactive think.
    assert len([c for c in executor.calls if c["name"] == "End"]) == 1
    assert rsp.presentation.cause_by == CauseBy.RUN_COMMAND.value


async def test_run_waits_on_pending_background_tasks(make_engine):
    # When think yields nothing but the bg pool is busy, the loop parks on
    # wait_any() instead of breaking, then re-observes and continues.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "End", "args": {}}])
    executor = _DeactExecutor("End")
    bg = FakeBgPool(pending=1)
    b = make_engine(channel=channel, executor=executor, bg_pool=bg)
    executor.holder = b.active
    _news(b)

    await b.engine.run()

    assert bg.wait_any_calls >= 1
    assert bg.pending == 0  # drained


async def test_run_acts_several_rounds_then_finishes(make_engine):
    # Native flow: the channel reports non-terminal for two rounds (so act runs
    # twice) and terminal on the third, so the loop finishes on that turn. The
    # is_terminal check is consulted once per think round, *before* act.
    engine = FakeThinkEngine(content="final text")
    channel = _SeqTerminalChannel(
        commands=[{"id": "t1", "command_name": "Read", "args": {}}],
        terminal_seq=[False, False, True],
    )
    executor = FakeExecutor(results={"Read": FakeResult(output="data")})
    b = make_engine(
        think_engine=engine,
        channel=channel,
        executor=executor,
    )
    _news(b)

    rsp = await b.engine.run()

    # Two acts ran (one Read each), then the terminal round finished.
    assert [c["name"] for c in executor.calls] == ["Read", "Read"]
    assert channel.is_terminal_calls == 3
    # Recorded turns: two act turns (with the command) + one finish turn (empty).
    assert len(channel.recorded_turns) == 3
    assert channel.recorded_turns[-1] == ("final text", [])
    # The finish path surfaces the assistant's plain text as the response.
    assert rsp.presentation.content == "final text"
    assert rsp.presentation.cause_by == CauseBy.RUN_COMMAND.value


async def test_run_terminal_checked_before_act_each_round(make_engine):
    # A terminal verdict on the very first round skips act entirely, even though
    # the channel has commands queued — the loop checks is_terminal first.
    channel = _SeqTerminalChannel(
        commands=[{"id": "t1", "command_name": "Read", "args": {}}],
        terminal_seq=[True],
    )
    executor = FakeExecutor()
    b = make_engine(channel=channel, executor=executor)
    _news(b)

    await b.engine.run()

    assert executor.calls == []
    assert channel.is_terminal_calls == 1


async def test_run_budget_stop_halts_before_think(make_engine):
    # A hard-cap verdict must break the loop *before* any think: the engine is
    # never started and no command runs. The verdict message becomes the reply.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "Read", "args": {}}])
    executor = FakeExecutor(results={"Read": FakeResult(output="data")})
    b = make_engine(channel=channel, executor=executor)
    b.provider.budget_verdict = BudgetVerdict(stop=True, message="budget-halt")
    _news(b)

    rsp = await b.engine.run()

    assert b.provider.enforce_budget_calls == 1
    assert b.provider.prepare_calls == 0  # never assembled a think request
    assert b.think_engine.start_calls == []  # never touched the LLM
    assert executor.calls == []  # never acted
    assert rsp.presentation.content == "budget-halt"
    assert rsp.presentation.cause_by == CauseBy.RUN_COMMAND.value


async def test_run_budget_proceed_allows_normal_act(make_engine):
    # The default PROCEED verdict is transparent: the loop thinks + acts as usual
    # and consults the gate on the turn it ran. A terminate result stops it after
    # one act so the test is bounded.
    channel = FakeChannel(commands=[{"id": "t1", "command_name": "Read", "args": {}}])
    executor = FakeExecutor(results={"Read": FakeResult(output="data", terminate=True)})
    b = make_engine(channel=channel, executor=executor)
    _news(b)

    rsp = await b.engine.run()

    # Gate consulted every turn: once before the act, once on the post-terminate
    # think that finds nothing to do and breaks.
    assert b.provider.enforce_budget_calls >= 1
    assert [c["name"] for c in executor.calls] == ["Read"]
    assert "data" in rsp.presentation.content
