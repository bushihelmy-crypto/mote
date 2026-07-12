#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.think.think_engine.ThinkEngine`.

Covers the two completion channels (XML text via ``aask`` vs native tool-use via
``aask_tool``), the dedup post-processing for each channel, the async task
lifecycle (``start`` / ``done`` / ``join``), and the ``BaseThinkEngine`` contract.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.common.const import TOOL_CALLS
from mote.common.schema import ThinkResult, UserMessage
from mote.think.think_engine import ThinkEngine

from .conftest import FakeLLM, FakeMemory, FakeReporter, history_with_calls, make_tool_response


def make_engine(*, memory=None, config=None) -> ThinkEngine:
    return ThinkEngine(memory=memory or FakeMemory(), config=config or {})


class TestConstruction:
    def test_initial_state(self):
        mem = FakeMemory()
        cfg = {"x": 1}
        engine = ThinkEngine(memory=mem, config=cfg)
        assert engine.llm is None
        assert engine.memory is mem
        assert engine.config is cfg
        assert isinstance(engine.result, ThinkResult)
        assert engine.result.content == ""
        assert engine.result.tool_calls is None

    def test_done_true_before_any_start(self):
        # No task pending -> done.
        assert make_engine().done is True

    def test_is_basethinkengine(self):
        from mote.common.base import BaseThinkEngine

        assert isinstance(make_engine(), BaseThinkEngine)


class TestXMLChannel:
    @pytest.mark.asyncio
    async def test_runs_and_produces_text_result(self):
        llm = FakeLLM(reply="my thought")
        engine = make_engine()
        await engine.start(req=[{"role": "user", "content": "hi"}], system_prompt="sys", llm=llm)
        await engine.join()
        assert engine.result.content == "my thought"
        # XML channel -> no structured tool calls.
        assert engine.result.tool_calls is None
        assert engine.result.is_native is False

    @pytest.mark.asyncio
    async def test_uses_aask_not_aask_tool(self):
        llm = FakeLLM(reply="t")
        engine = make_engine()
        await engine.start(req="REQ", system_prompt="SYS", llm=llm)
        await engine.join()
        assert len(llm.aask_calls) == 1
        assert llm.aask_calls[0]["msg"] == "REQ"
        assert llm.aask_calls[0]["system_msgs"] == ["SYS"]
        assert llm.aask_tool_calls == []

    @pytest.mark.asyncio
    async def test_start_assigns_llm(self):
        llm = FakeLLM()
        engine = make_engine()
        await engine.start(req="r", system_prompt="s", llm=llm)
        assert engine.llm is llm
        await engine.join()

    @pytest.mark.asyncio
    async def test_no_duplicate_passes_through(self):
        # History has unrelated content -> dedup leaves the response untouched.
        mem = FakeMemory([UserMessage(content="something else")])
        llm = FakeLLM(reply="fresh thought")
        engine = make_engine(memory=mem)
        await engine.start(req="r", system_prompt="s", llm=llm)
        await engine.join()
        assert engine.result.content == "fresh thought"

    @pytest.mark.asyncio
    async def test_hard_repeat_triggers_ask_user(self):
        # Same response present 3x in recent history -> ask-human override.
        dup = "I will look at the file again"
        mem = FakeMemory([UserMessage(content=dup) for _ in range(3)])
        llm = FakeLLM(reply=dup)
        engine = make_engine(memory=mem)
        await engine.start(req=[{"role": "user", "content": "go"}], system_prompt="s", llm=llm)
        await engine.join()
        # Overrides with a synthesized AskUserQuestion call (the registered tool),
        # not the unregistered ask_user (which the loop would filter out).
        assert "AskUserQuestion" in engine.result.content
        # The dedup path asks the LLM a second time to summarize the problem.
        assert len(llm.aask_calls) == 2


class TestNativeChannel:
    @pytest.mark.asyncio
    async def test_maps_tool_call_fields(self):
        resp = make_tool_response(("call-1", "Read", {"path": "a.py"}), content="reading")
        llm = FakeLLM(tool_response=resp)
        engine = make_engine()
        await engine.start(req="r", system_prompt="s", tool_specs=[{"name": "Read"}], llm=llm)
        await engine.join()
        assert engine.result.content == "reading"
        assert engine.result.is_native is True
        assert engine.result.tool_calls == [{"id": "call-1", "command_name": "Read", "args": {"path": "a.py"}}]

    @pytest.mark.asyncio
    async def test_uses_aask_tool_with_specs(self):
        llm = FakeLLM(tool_response=make_tool_response(("1", "Glob", {})))
        engine = make_engine()
        specs = [{"name": "Glob"}]
        await engine.start(req="REQ", system_prompt="SYS", tool_specs=specs, llm=llm)
        await engine.join()
        assert len(llm.aask_tool_calls) == 1
        assert llm.aask_tool_calls[0]["tools"] is specs
        assert llm.aask_tool_calls[0]["system_msgs"] == ["SYS"]
        assert llm.aask_calls == []

    @pytest.mark.asyncio
    async def test_empty_content_becomes_empty_string(self):
        # rsp.content is None -> normalized to "" but still native (tool_calls list).
        llm = FakeLLM(tool_response=make_tool_response(("1", "Bash", {"cmd": "ls"})))
        llm._tool_response.content = None
        engine = make_engine()
        await engine.start(req="r", system_prompt="s", tool_specs=[{"name": "Bash"}], llm=llm)
        await engine.join()
        assert engine.result.content == ""
        assert engine.result.is_native is True

    @pytest.mark.asyncio
    async def test_no_tool_calls_yields_empty_list(self):
        # Native channel with zero calls -> tool_calls == [] (still is_native).
        llm = FakeLLM(tool_response=make_tool_response(content="just text"))
        engine = make_engine()
        await engine.start(req="r", system_prompt="s", tool_specs=[{"name": "X"}], llm=llm)
        await engine.join()
        assert engine.result.tool_calls == []
        assert engine.result.is_native is True

    @pytest.mark.asyncio
    async def test_unique_calls_not_overridden(self):
        # History signatures don't match -> keep the original calls.
        mem = FakeMemory(history_with_calls([{"name": "Read", "args": {"path": "z"}}]))
        llm = FakeLLM(tool_response=make_tool_response(("1", "Read", {"path": "a"})))
        engine = make_engine(memory=mem)
        await engine.start(req="r", system_prompt="s", tool_specs=[{"name": "Read"}], llm=llm)
        await engine.join()
        assert engine.result.tool_calls == [{"id": "1", "command_name": "Read", "args": {"path": "a"}}]

    @pytest.mark.asyncio
    async def test_hard_repeat_overrides_with_ask_user(self):
        # Same call signature 3x in history -> override with a synthesized
        # AskUserQuestion call (the registered tool, not unregistered ask_user).
        sig = [{"name": "Editor", "args": {"path": "a"}}]
        mem = FakeMemory(history_with_calls(sig, sig, sig))
        llm = FakeLLM(tool_response=make_tool_response(("1", "Editor", {"path": "a"})))
        engine = make_engine(memory=mem)
        await engine.start(
            req=[{"role": "user", "content": "go"}], system_prompt="s", tool_specs=[{"name": "Editor"}], llm=llm
        )
        await engine.join()
        assert len(engine.result.tool_calls) == 1
        override = engine.result.tool_calls[0]
        assert override["command_name"] == "AskUserQuestion"
        assert "questions" in override["args"]

    @pytest.mark.asyncio
    async def test_sig_hist_ignores_messages_without_tool_calls(self):
        # Plain messages (no TOOL_CALLS metadata) must not count toward repeats.
        sig = [{"name": "Editor", "args": {"path": "a"}}]
        mem = FakeMemory([UserMessage(content="noise")] * 5 + history_with_calls(sig, sig))
        llm = FakeLLM(tool_response=make_tool_response(("1", "Editor", {"path": "a"})))
        engine = make_engine(memory=mem)
        await engine.start(req="r", system_prompt="s", tool_specs=[{"name": "Editor"}], llm=llm)
        await engine.join()
        # Only 2 matching signatures (< 3) -> no override.
        assert engine.result.tool_calls[0]["command_name"] == "Editor"

    @pytest.mark.asyncio
    async def test_end_call_repeat_not_overridden(self):
        # "End" repeats are legitimate and skipped by the dedup guard.
        sig = [{"name": "End", "args": {}}]
        mem = FakeMemory(history_with_calls(sig, sig, sig))
        llm = FakeLLM(tool_response=make_tool_response(("1", "End", {})))
        engine = make_engine(memory=mem)
        await engine.start(req="r", system_prompt="s", tool_specs=[{"name": "End"}], llm=llm)
        await engine.join()
        assert engine.result.tool_calls[0]["command_name"] == "End"


class TestTaskLifecycle:
    @pytest.mark.asyncio
    async def test_done_false_after_start_true_after_join(self):
        # start schedules the task but yields control before it runs.
        llm = FakeLLM()
        engine = make_engine()
        await engine.start(req="r", system_prompt="s", llm=llm)
        assert engine.done is False
        await engine.join()
        assert engine.done is True

    @pytest.mark.asyncio
    async def test_join_clears_task(self):
        engine = make_engine()
        await engine.start(req="r", system_prompt="s", llm=FakeLLM())
        await engine.join()
        assert engine._task is None

    @pytest.mark.asyncio
    async def test_join_noop_without_task(self):
        engine = make_engine()
        # Should not raise when there is nothing to await.
        await engine.join()
        assert engine.done is True

    @pytest.mark.asyncio
    async def test_result_replaced_each_round(self):
        engine = make_engine()
        await engine.start(req="r", system_prompt="s", llm=FakeLLM(reply="one"))
        await engine.join()
        first = engine.result
        await engine.start(req="r", system_prompt="s", llm=FakeLLM(reply="two"))
        await engine.join()
        assert engine.result is not first
        assert engine.result.content == "two"


class TestReporter:
    @pytest.mark.asyncio
    async def test_emits_react_report(self):
        engine = make_engine()
        await engine.start(req="r", system_prompt="s", llm=FakeLLM())
        await engine.join()
        assert FakeReporter.instances, "ThoughtReporter should be entered"
        reporter = FakeReporter.instances[0]
        assert reporter.kwargs.get("enable_llm_stream") is True
        assert ({"type": "react"}, "object") in reporter.reports
