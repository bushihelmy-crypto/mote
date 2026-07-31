#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ExecutionEngine durable-think wiring (G1 re-pay guard).

The loop memoizes each think round in the shared run journal via a
:class:`InferenceJournal`, so a resume can reinstate a completed think (skip the
model) instead of re-paying it. These tests drive flow services against a real
journal and assert the
journal lifecycle: begin (started) → complete (payload) → reap (gone), plus the
reinstate branch that adopts a recovered result without launching the LLM.

When no runner is injected the durable hooks are no-ops — the pre-A3 path.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.contracts.conversation import AIMessage, UserMessage
from mote.contracts.model.inference import InferenceResult
from mote.runtime.durable import InferenceJournal
from mote.runtime.durable.backend import JsonlBackend
from mote.runtime.ledger import COMPLETED, STARTED, RunJournal
from mote.runtime.session.workspace import SessionWorkspace

from .conftest import FakeChannel, FakeExecutor, FakeResult, FakeThinkEngine

pytestmark = pytest.mark.asyncio


def _runner(tmp_path, session_id="sess") -> InferenceJournal:
    journal = RunJournal(session_id, store=SessionWorkspace(root=str(tmp_path)))
    return InferenceJournal(JsonlBackend(journal))


def _cmd(name, *, id=None, args=None) -> dict:
    return {"id": id, "command_name": name, "args": args or {}}


def _engine(content: str = "the thought", tool_calls=None) -> FakeThinkEngine:
    eng = FakeThinkEngine()
    eng.result = InferenceResult(content=content, tool_calls=tool_calls)
    return eng


# ----------------------------------------------------------------------
# begin_think — a fresh LLM think allocates + records a started record
# ----------------------------------------------------------------------


async def test_think_begins_journal_record(make_engine, tmp_path):
    import json

    runner = _runner(tmp_path)
    b = make_engine(inference_engine=_engine(), durable_runner=runner)
    b.engine._ctx = b.ctx

    ran = await b.engine._inference.infer()

    assert ran is True
    assert b.engine._inference_checkpoint.step_id == "think:1"
    rec = runner.journal.replay("think:1")
    assert rec is not None and rec.status == STARTED
    checkpoint = json.loads(rec.payload)["checkpoint"]
    assert checkpoint["model_call_id"]
    assert checkpoint["target_lease_id"] == "fake-lease"
    assert checkpoint["capability_fingerprint"] == "fake-capabilities"
    assert checkpoint["tool_snapshot_id"] == "fake-snapshot"
    assert checkpoint["tool_registry_revision"] == 1
    assert checkpoint["request_fingerprint"]
    # A normal (non-reinstate) think still launches the model.
    assert b.inference_engine.start_calls and b.inference_engine.reinstated == []


async def test_no_runner_leaves_journal_untouched(make_engine):
    # Without a durable runner every hook is a no-op — the pre-A3 path.
    b = make_engine(inference_engine=_engine())
    b.engine._ctx = b.ctx

    await b.engine._inference.infer()

    assert b.engine._inference_checkpoint.step_id is None
    assert b.inference_engine.start_calls  # still thinks normally


# ----------------------------------------------------------------------
# complete + reap across a full think→act
# ----------------------------------------------------------------------


async def test_act_completes_then_reaps_record(make_engine, tmp_path):
    runner = _runner(tmp_path)
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    b = make_engine(inference_engine=_engine("thought"), channel=channel, durable_runner=runner)
    b.engine._ctx = b.ctx

    # Simulate the think round that _step_think would have allocated.
    b.engine._inference_checkpoint.adopt_started(runner.begin_think())

    await b.engine._actions.execute()

    # After the assistant message is recorded, the think record is reaped and the
    # per-round durable state cleared.
    assert runner.journal.replay("think:1") is None
    assert b.engine._inference_checkpoint.step_id is None
    assert b.engine._inference_checkpoint.reinstated is False


async def test_finish_stages_then_terminal_commit_reaps_record(make_engine, tmp_path):
    from mote.contracts.model.turn import FinalCandidateAction

    runner = _runner(tmp_path)
    b = make_engine(inference_engine=_engine("final answer"), durable_runner=runner)
    b.engine._ctx = b.ctx
    b.engine._inference_checkpoint.adopt_started(runner.begin_think())

    candidate = FinalCandidateAction(raw="final answer", representation="native_text")
    await b.engine._outputs.evaluate(candidate)
    await b.engine._outputs.accept(candidate)

    assert runner.journal.replay("think:1") is not None
    await b.engine._outputs.commit()

    assert runner.journal.replay("think:1") is None
    assert b.engine._inference_checkpoint.step_id is None


# ----------------------------------------------------------------------
# reinstate — adopt a recovered result without launching the LLM
# ----------------------------------------------------------------------


async def test_think_reinstates_completed_candidate(make_engine, tmp_path):
    runner = _runner(tmp_path)
    # Pre-seed a completed think whose assistant message never reached history.
    step_id = runner.begin_think()
    runner.complete_think(step_id, InferenceResult(content="recovered", tool_calls=None))

    b = make_engine(inference_engine=_engine("stale"), durable_runner=runner)
    b.engine._ctx = b.ctx

    ran = await b.engine._inference.infer()

    assert ran is True
    # The LLM was NOT launched; the engine adopted the recovered result.
    assert b.inference_engine.start_calls == []
    assert b.inference_engine.reinstated and b.inference_engine.reinstated[0].content == "recovered"
    assert b.engine._inference_checkpoint.step_id == step_id
    assert b.engine._inference_checkpoint.reinstated is True


async def test_reinstated_round_is_not_recompleted_then_reaped(make_engine, tmp_path):
    runner = _runner(tmp_path)
    step_id = runner.begin_think()
    runner.complete_think(step_id, InferenceResult(content="recovered", tool_calls=None))

    channel = FakeChannel(commands=[])  # terminal-style: no commands
    b = make_engine(inference_engine=_engine("recovered"), channel=channel, durable_runner=runner)
    b.engine._ctx = b.ctx

    await b.engine._inference.infer()  # reinstate
    # complete_think must be SKIPPED for a reinstated round (record already
    # completed) — assert by ensuring the record still exists as completed
    # right up until _step_act reaps it.
    assert runner.journal.replay(step_id).status == COMPLETED

    await b.engine._actions.execute()

    assert runner.journal.replay(step_id) is None  # reaped after recording


async def test_reinstate_skipped_when_no_candidate(make_engine, tmp_path):
    runner = _runner(tmp_path)
    # Completed think whose assistant message IS already in history → no candidate.
    step_id = runner.begin_think()
    runner.complete_think(step_id, InferenceResult(content="done", tool_calls=None))
    b = make_engine(
        inference_engine=_engine("done"),
        durable_runner=runner,
        memory=None,
    )
    # Seed history with the matching assistant message.
    await b.memory.add(AIMessage(content="done"))
    b.engine._ctx = b.ctx

    ran = await b.engine._inference.infer()

    assert ran is True
    # No reinstate → a fresh think began (new seq) and the LLM launched.
    assert b.inference_engine.reinstated == []
    assert b.inference_engine.start_calls
    assert b.engine._inference_checkpoint.step_id == "think:2"


# ----------------------------------------------------------------------
# checkpoint (EXTERNAL) path still reaps after record_results
# ----------------------------------------------------------------------


async def test_checkpoint_path_reaps_after_results(make_engine, tmp_path):
    from .conftest import FakeExecutor, FakeResult

    runner = _runner(tmp_path)
    channel = FakeChannel(commands=[_cmd("Bash", id="t1", args={"cmd": "echo hi"})])
    executor = FakeExecutor(results={"Bash": FakeResult(output="hi")}, ledgered={"Bash"})
    b = make_engine(
        inference_engine=_engine("thought"),
        channel=channel,
        executor=executor,
        durable_runner=runner,
    )
    b.engine._ctx = b.ctx
    b.engine._inference_checkpoint.adopt_started(runner.begin_think())

    await b.engine._actions.execute()

    # The EXTERNAL checkpoint path records call + results; the think record is
    # reaped once the assistant message is durable.
    assert channel.recorded_calls and channel.recorded_results
    assert runner.journal.replay("think:1") is None


# ----------------------------------------------------------------------
# Turn-boundary INVARIANT: a user interrupt (CancelledError) must terminally
# resolve the round's journal record so a later resume never mistakes an
# ABANDONED round for a crash and reinstates its stale plan. Regression for the
# "cancelled but retried anyway" bug: a non-checkpoint (read-only tool) round
# left its completed think in the journal, and the next turn replayed it instead
# of freshly thinking on the user's new message.
# ----------------------------------------------------------------------


class _RaisingExecutor(FakeExecutor):
    """Executor that raises a chosen exception when a named command runs.

    Models an interrupt (``CancelledError``) landing at the ``await`` inside
    run_command mid-turn — the exact shape ``AgentControl.interrupt`` produces.
    """

    def __init__(self, *, raise_on: str, exc: BaseException, **kwargs):
        super().__init__(**kwargs)
        self._raise_on = raise_on
        self._exc = exc

    async def run_command(self, name, args, result_id=None):
        self.calls.append({"name": name, "args": args, "result_id": result_id})
        if name == self._raise_on:
            raise self._exc
        return self.results.get(name, self.default)


def _news(b, *, name="Alice") -> None:
    """Push one message addressed to the role so the initial observe fires."""
    b.buffer.push(UserMessage("go", send_to={name}))


async def test_run_non_checkpoint_interrupt_reaps_think(make_engine, tmp_path):
    # THE bug: a read-only (non-ledgered → non-checkpoint) round whose act is
    # interrupted mid-body. The think already recorded ``completed``, but the
    # assistant message never landed (record_turn runs only on the success tail).
    # The turn boundary must reap the record so a later reinstate_candidate does
    # NOT resurrect the stale plan — a user cancel is an abandoned round, not a
    # crash to recover.
    runner = _runner(tmp_path)
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    executor = _RaisingExecutor(raise_on="Read", exc=asyncio.CancelledError())  # nothing ledgered
    b = make_engine(
        inference_engine=_engine("plan", tool_calls=None),
        channel=channel,
        executor=executor,
        durable_runner=runner,
    )
    _news(b)

    with pytest.raises(asyncio.CancelledError):
        await b.engine.run()

    # No completed think lingers → the next run thinks fresh instead of replaying.
    assert runner.journal.replay("think:1") is None
    assert runner.reinstate_candidate([]) is None


async def test_run_checkpoint_interrupt_reaps_think(make_engine, tmp_path):
    # The EXTERNAL (checkpoint) path is interrupted mid-body. Its assistant call
    # was recorded up front (durable), the pairing is closed with [INTERRUPTED]
    # results by _step_act, and the turn boundary still reaps the think record —
    # the same terminal resolution as the non-checkpoint path.
    runner = _runner(tmp_path)
    channel = FakeChannel(commands=[_cmd("Bash", id="t1")])
    executor = _RaisingExecutor(raise_on="Bash", exc=asyncio.CancelledError(), ledgered={"Bash"})
    b = make_engine(
        inference_engine=_engine("plan"),
        channel=channel,
        executor=executor,
        durable_runner=runner,
    )
    _news(b)

    with pytest.raises(asyncio.CancelledError):
        await b.engine.run()

    # Pairing closed up front (checkpoint), think record reaped by the boundary.
    assert channel.recorded_calls and channel.recorded_results
    assert runner.journal.replay("think:1") is None
    assert runner.reinstate_candidate([]) is None


async def test_run_think_phase_interrupt_reaps_started_record(make_engine, tmp_path):
    # A cancel landing during the THINK phase (LLM start) — the round has only a
    # ``started`` record, no completed result. Widening the turn's try to cover
    # _step_think means this is reaped in-process too, rather than leaking a
    # dangling ``started`` until a future resume reconciles it.
    runner = _runner(tmp_path)
    engine = _engine("plan")

    async def _cancel_start(*args, **kwargs):
        raise asyncio.CancelledError()

    engine.start = _cancel_start  # type: ignore[assignment]
    b = make_engine(inference_engine=engine, durable_runner=runner)
    _news(b)

    with pytest.raises(asyncio.CancelledError):
        await b.engine.run()

    # The started record allocated by begin_think was reaped by the boundary.
    assert runner.journal.replay("think:1") is None


async def test_run_failure_still_fails_think(make_engine, tmp_path):
    # A non-interrupt Exception (e.g. LLM recovery exhausted) still records the
    # round ``failed`` then reaps it — the failure twin of the cancel path, kept
    # intact by the widened try. Distinct from cancel so observability stays
    # honest (failed leaves a breadcrumb event; cancel is a clean abandon).
    runner = _runner(tmp_path)
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    executor = _RaisingExecutor(raise_on="Read", exc=RuntimeError("boom"))
    b = make_engine(
        inference_engine=_engine("plan"),
        channel=channel,
        executor=executor,
        durable_runner=runner,
    )
    _news(b)

    with pytest.raises(RuntimeError):
        await b.engine.run()

    assert runner.journal.replay("think:1") is None
    assert runner.reinstate_candidate([]) is None
