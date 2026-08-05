#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``ActionExecutionService`` — drain commands, execute, record.

Key behaviours:
- commands are executed in order; the tool-use id flows through as ``result_id``.
- on the FIRST failure the remaining commands are NOT executed but each still
  gets a synthesized ``[SKIPPED]`` result (native tool-use needs every tool_call
  paired with a tool_result).
- media (images / pdfs) on a result is surfaced onto the executed entry.
- the channel ``record_turn`` is called with the think content + executed list,
  and the think round is joined.
- no commands -> a "No valid commands found" notice is recorded.
"""

from __future__ import annotations

import asyncio

import pytest

from mote.contracts.conversation import CauseBy
from mote.ztest.artifact_fakes import artifact_media

from .conftest import FakeChannel, FakeExecutor, FakeResult, FakeThinkEngine

pytestmark = pytest.mark.asyncio


def _cmd(name, *, id=None, args=None) -> dict:
    return {"id": id, "command_name": name, "args": args or {}}


async def test_act_executes_in_order_and_passes_result_id(make_engine):
    channel = FakeChannel(commands=[_cmd("Read", id="t1", args={"path": "a"}), _cmd("Glob", id="t2")])
    executor = FakeExecutor(
        results={
            "Read": FakeResult(output="readout"),
            "Glob": FakeResult(output="globout"),
        }
    )
    b = make_engine(channel=channel, executor=executor)
    b.engine._ctx = b.ctx

    rsp = await b.engine._actions.execute()

    # Both ran, in order, with their tool-use ids forwarded as result_id.
    assert [c["name"] for c in executor.calls] == ["Read", "Glob"]
    assert executor.calls[0]["result_id"] == "t1"
    assert executor.calls[1]["result_id"] == "t2"

    content, executed = channel.recorded_turns[0]
    assert [entry.name for entry in executed] == ["Read", "Glob"]
    assert all(entry.success for entry in executed)
    assert "readout" in rsp.content and "globout" in rsp.content
    assert rsp.cause_by == CauseBy.RUN_COMMAND.value
    assert rsp.sent_from == b.ctx.name
    assert b.inference_engine.join_calls == 1


async def test_act_first_failure_skips_remaining(make_engine):
    channel = FakeChannel(commands=[_cmd("Read", id="t1"), _cmd("Glob", id="t2"), _cmd("Grep", id="t3")])
    executor = FakeExecutor(
        results={"Read": FakeResult(output="boom", success=False)},
        default=FakeResult(output="shouldnotrun"),
    )
    b = make_engine(channel=channel, executor=executor)
    b.engine._ctx = b.ctx

    await b.engine._actions.execute()

    # Only the failing command actually executed.
    assert [c["name"] for c in executor.calls] == ["Read"]

    _, executed = channel.recorded_turns[0]
    assert executed[0].success is False
    # Remaining are synthesized SKIPPED entries.
    for entry in executed[1:]:
        assert entry.success is False
        assert "[SKIPPED]" in entry.output


async def test_act_terminate_result_clears_active_signal(make_engine):
    # A result flagged ``terminate`` (user rejected the approval prompt) trips the
    # same kill switch the End tool uses: the loop clears the active signal so the
    # next think step returns False and the loop stops.
    channel = FakeChannel(commands=[_cmd("Bash", id="t1", args={"cmd": "rm -rf /"})])
    executor = FakeExecutor(results={"Bash": FakeResult(output="denied", success=False, terminate=True)})
    b = make_engine(channel=channel, executor=executor, active=True)
    b.engine._ctx = b.ctx

    assert b.active[0] is True
    await b.engine._actions.execute()
    assert b.active[0] is False


async def test_act_recoverable_failure_keeps_active_signal(make_engine):
    # A plain failure (no ``terminate``) must NOT end the loop: the model can
    # replan around it, so the active signal stays on.
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    executor = FakeExecutor(results={"Read": FakeResult(output="boom", success=False)})
    b = make_engine(channel=channel, executor=executor, active=True)
    b.engine._ctx = b.ctx

    await b.engine._actions.execute()
    assert b.active[0] is True


async def test_act_propagates_media(make_engine):
    image = artifact_media("image", "b64img")
    pdf = artifact_media("pdf", "b64pdf")
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    executor = FakeExecutor(results={"Read": FakeResult(output="img", media=[image, pdf])})
    b = make_engine(channel=channel, executor=executor)
    b.engine._ctx = b.ctx

    await b.engine._actions.execute()

    _, executed = channel.recorded_turns[0]
    assert executed[0].media == [image, pdf]


async def test_act_keeps_structured_media_as_the_authoritative_entry(make_engine):
    media = artifact_media("image", "b64img")
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    executor = FakeExecutor(results={"Read": FakeResult(output="img", media=[media])})
    b = make_engine(channel=channel, executor=executor)
    b.engine._ctx = b.ctx

    await b.engine._actions.execute()

    _, executed = channel.recorded_turns[0]
    assert executed[0].media == [media]
    assert not hasattr(executed[0], "images")
    assert not hasattr(executed[0], "pdfs")


async def test_act_no_commands_records_notice(make_engine):
    channel = FakeChannel(commands=[])
    b = make_engine(channel=channel)
    b.engine._ctx = b.ctx

    rsp = await b.engine._actions.execute()

    content, executed = channel.recorded_turns[0]
    assert executed == []
    assert "No valid commands found" in rsp.content
    assert b.inference_engine.join_calls == 1


async def test_act_passes_think_content_to_record_turn(make_engine):
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    engine = FakeThinkEngine(content="assistant reasoning")
    b = make_engine(channel=channel, inference_engine=engine)
    b.engine._ctx = b.ctx

    await b.engine._actions.execute()

    content, _ = channel.recorded_turns[0]
    assert content == "assistant reasoning"


async def test_act_dispatcher_filters_actions_with_context_tools(make_engine):
    channel = FakeChannel(commands=[_cmd("Read", id="t1"), _cmd("Unknown", id="t2")])
    b = make_engine(channel=channel, tools=["Read", "Glob", "AskUserQuestion"])
    b.engine._ctx = b.ctx

    await b.engine._actions.execute()

    assert [call["name"] for call in b.executor.calls] == ["Read"]
    assert channel.iter_calls == []


# ---------------------------------------------------------------------------
# Pre-execution durability checkpoint (EXTERNAL-effect tools)
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Stand-in for the process DiskWriter — records drain() barrier calls."""

    def __init__(self):
        self.drain_calls = 0

    async def drain(self) -> None:
        self.drain_calls += 1


async def test_act_external_checkpoint_records_call_and_drains_before_execution(
    make_engine,
):
    # An EXTERNAL-effect tool the executor would ledger: the loop must persist
    # the assistant tool-call message and flush it to disk BEFORE the body runs,
    # then record only the results afterwards (no single-shot record_turn).
    channel = FakeChannel(commands=[_cmd("Bash", id="t1", args={"cmd": "curl x"})])
    executor = FakeExecutor(results={"Bash": FakeResult(output="done")}, ledgered={"Bash"})
    writer = _FakeWriter()
    b = make_engine(channel=channel, executor=executor, drain_writes=writer.drain)
    b.engine._ctx = b.ctx

    await b.engine._actions.execute()

    # The call was recorded (once) and the disk flushed (once) before execution:
    # the snapshot taken at record_call time still has an empty, un-run output.
    assert len(channel.recorded_calls) == 1
    _, snap = channel.recorded_calls[0]
    assert snap[0].output == ""
    assert writer.drain_calls == 1
    # Results recorded after, with the real output; the single-shot path is skipped.
    assert channel.recorded_results and channel.recorded_results[0][0].output == "done"
    assert channel.recorded_turns == []
    assert executor.calls[0]["name"] == "Bash"


async def test_act_non_external_turn_skips_checkpoint(make_engine):
    # A turn with no ledgered tool is committed as one transaction after effects.
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    executor = FakeExecutor(results={"Read": FakeResult(output="ok")})  # nothing ledgered
    writer = _FakeWriter()
    b = make_engine(channel=channel, executor=executor, drain_writes=writer.drain)
    b.engine._ctx = b.ctx

    await b.engine._actions.execute()

    assert channel.recorded_calls == []
    assert channel.recorded_results == []
    assert len(channel.recorded_turns) == 1
    assert writer.drain_calls == 0


# ---------------------------------------------------------------------------
# Mid-execution interrupt (Ctrl+C -> CancelledError) — pairing must be closed
# ---------------------------------------------------------------------------


class _RaisingExecutor(FakeExecutor):
    """FakeExecutor that raises a chosen exception when a named command runs.

    Models an interrupt (``CancelledError``) landing at the ``await`` inside
    run_command mid-turn — the exact shape AgentControl.interrupt produces.
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


async def test_act_checkpoint_interrupt_closes_pairing_then_reraises(make_engine):
    # An EXTERNAL (ledgered -> checkpoint) turn whose SECOND call is interrupted
    # by a Ctrl+C (CancelledError) mid-body. The assistant tool_call message was
    # already recorded up front, so leaving now would strand two tool_use ids
    # without tool_results (the very thing that 400s the next request). The loop
    # must synthesize a result for every UNSETTLED call, record all results to
    # close the pairing, then re-raise so the normal interrupt unwind proceeds.
    channel = FakeChannel(
        commands=[
            _cmd("RunGraph", id="t1"),
            _cmd("RunGraph", id="t2"),
            _cmd("RunGraph", id="t3"),
        ]
    )
    executor = FakeExecutor(default=FakeResult(output="done"), ledgered={"RunGraph"})
    # First call settles normally; the SECOND raises CancelledError mid-body.
    calls_seen = {"n": 0}

    async def _run(name, args, result_id=None):
        executor.calls.append({"name": name, "args": args, "result_id": result_id})
        calls_seen["n"] += 1
        if calls_seen["n"] == 2:
            raise asyncio.CancelledError()
        return FakeResult(output="done")

    executor.run_command = _run  # type: ignore[assignment]

    writer = _FakeWriter()
    b = make_engine(channel=channel, executor=executor, drain_writes=writer.drain)
    b.engine._ctx = b.ctx

    with pytest.raises(asyncio.CancelledError):
        await b.engine._actions.execute()

    # The assistant call was checkpointed; results were recorded despite the
    # interrupt, so EVERY emitted tool_call id now has a paired result.
    assert len(channel.recorded_results) == 1
    recorded = channel.recorded_results[0]
    assert [entry.action_id for entry in recorded] == ["t1", "t2", "t3"]
    # t1 ran to completion; t2 (interrupted) and t3 (never reached) are closed
    # with an INTERRUPTED marker and marked unsuccessful.
    assert recorded[0].output == "done" and recorded[0].success is True
    assert "[INTERRUPTED]" in recorded[1].output and recorded[1].success is False
    assert "[INTERRUPTED]" in recorded[2].output and recorded[2].success is False
    # The single-shot record_turn was NOT used (checkpoint path re-raised).
    assert channel.recorded_turns == []


async def test_act_non_checkpoint_interrupt_records_nothing(make_engine):
    # A non-ledgered (non-checkpoint) turn interrupted mid-body: nothing was
    # recorded yet (record_turn runs only on the success tail), so there is no
    # dangling tool_use to repair. The loop must simply propagate the interrupt
    # without recording a partial turn.
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    executor = _RaisingExecutor(
        raise_on="Read", exc=asyncio.CancelledError(), results={}, default=FakeResult()
    )  # nothing ledgered -> no checkpoint
    writer = _FakeWriter()
    b = make_engine(channel=channel, executor=executor, drain_writes=writer.drain)
    b.engine._ctx = b.ctx

    with pytest.raises(asyncio.CancelledError):
        await b.engine._actions.execute()

    assert channel.recorded_calls == []
    assert channel.recorded_results == []
    assert channel.recorded_turns == []
