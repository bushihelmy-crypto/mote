#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``ReActLoop._step_act`` — drain commands, execute, record.

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

import pytest

from metagpt.common.schema import CauseBy

from .conftest import FakeChannel, FakeExecutor, FakeResult, FakeThinkEngine

pytestmark = pytest.mark.asyncio


def _cmd(name, *, id=None, args=None) -> dict:
    return {"id": id, "command_name": name, "args": args or {}}


async def test_act_executes_in_order_and_passes_result_id(make_loop):
    channel = FakeChannel(
        commands=[_cmd("Read", id="t1", args={"path": "a"}), _cmd("Glob", id="t2")]
    )
    executor = FakeExecutor(
        results={"Read": FakeResult(output="readout"), "Glob": FakeResult(output="globout")}
    )
    b = make_loop(channel=channel, executor=executor)
    b.loop._ctx = b.ctx

    rsp = await b.loop._step_act()

    # Both ran, in order, with their tool-use ids forwarded as result_id.
    assert [c["name"] for c in executor.calls] == ["Read", "Glob"]
    assert executor.calls[0]["result_id"] == "t1"
    assert executor.calls[1]["result_id"] == "t2"

    content, executed = channel.recorded_turns[0]
    assert [e["name"] for e in executed] == ["Read", "Glob"]
    assert all(e["success"] for e in executed)
    assert "readout" in rsp.content and "globout" in rsp.content
    assert rsp.cause_by == CauseBy.RUN_COMMAND.value
    assert rsp.sent_from == b.ctx.name
    assert b.think_engine.join_calls == 1


async def test_act_first_failure_skips_remaining(make_loop):
    channel = FakeChannel(
        commands=[_cmd("Read", id="t1"), _cmd("Glob", id="t2"), _cmd("Grep", id="t3")]
    )
    executor = FakeExecutor(
        results={"Read": FakeResult(output="boom", success=False)},
        default=FakeResult(output="shouldnotrun"),
    )
    b = make_loop(channel=channel, executor=executor)
    b.loop._ctx = b.ctx

    await b.loop._step_act()

    # Only the failing command actually executed.
    assert [c["name"] for c in executor.calls] == ["Read"]

    _, executed = channel.recorded_turns[0]
    assert executed[0]["success"] is False
    # Remaining are synthesized SKIPPED entries.
    for entry in executed[1:]:
        assert entry["success"] is False
        assert "[SKIPPED]" in entry["output"]


async def test_act_terminate_result_clears_active_signal(make_loop):
    # A result flagged ``terminate`` (user rejected the approval prompt) trips the
    # same kill switch the End tool uses: the loop clears the active signal so the
    # next think step returns False and the loop stops.
    channel = FakeChannel(commands=[_cmd("Bash", id="t1", args={"cmd": "rm -rf /"})])
    executor = FakeExecutor(
        results={"Bash": FakeResult(output="denied", success=False, terminate=True)}
    )
    b = make_loop(channel=channel, executor=executor, active=True)
    b.loop._ctx = b.ctx

    assert b.active[0] is True
    await b.loop._step_act()
    assert b.active[0] is False


async def test_act_recoverable_failure_keeps_active_signal(make_loop):
    # A plain failure (no ``terminate``) must NOT end the loop: the model can
    # replan around it, so the active signal stays on.
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    executor = FakeExecutor(results={"Read": FakeResult(output="boom", success=False)})
    b = make_loop(channel=channel, executor=executor, active=True)
    b.loop._ctx = b.ctx

    await b.loop._step_act()
    assert b.active[0] is True


async def test_act_propagates_media(make_loop):
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    executor = FakeExecutor(
        results={"Read": FakeResult(output="img", images=["b64img"], pdfs=["b64pdf"])}
    )
    b = make_loop(channel=channel, executor=executor)
    b.loop._ctx = b.ctx

    await b.loop._step_act()

    _, executed = channel.recorded_turns[0]
    assert executed[0]["images"] == ["b64img"]
    assert executed[0]["pdfs"] == ["b64pdf"]


async def test_act_no_commands_records_notice(make_loop):
    channel = FakeChannel(commands=[])
    b = make_loop(channel=channel)
    b.loop._ctx = b.ctx

    rsp = await b.loop._step_act()

    content, executed = channel.recorded_turns[0]
    assert executed == []
    assert "No valid commands found" in rsp.content
    assert b.think_engine.join_calls == 1


async def test_act_passes_think_content_to_record_turn(make_loop):
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    engine = FakeThinkEngine(content="assistant reasoning")
    b = make_loop(channel=channel, think_engine=engine)
    b.loop._ctx = b.ctx

    await b.loop._step_act()

    content, _ = channel.recorded_turns[0]
    assert content == "assistant reasoning"


async def test_act_iter_commands_gets_valid_names(make_loop):
    channel = FakeChannel(commands=[_cmd("Read", id="t1")])
    b = make_loop(channel=channel, tools=["Read", "Glob", "AskUserQuestion"])
    b.loop._ctx = b.ctx

    await b.loop._step_act()

    assert channel.iter_calls[0] == {"Read", "Glob", "AskUserQuestion"}
