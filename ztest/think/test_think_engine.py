#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.kernel.inference.engine.InferenceEngine`.

Covers the two completion channels (XML text via ``aask`` vs native tool-use via
``aask_tool``), the async task lifecycle (``start`` / ``done`` / ``join``), and
the ``BaseInferenceEngine`` contract.
"""

from __future__ import annotations

import asyncio

import pytest

from mote.contracts.conversation import UserMessage
from mote.contracts.conversation.fields import TOOL_CALLS
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.invocation import CanonicalToolDefinition
from mote.kernel.inference.engine import InferenceEngine
from mote.runtime.models.inference_port import RuntimeModelInferencePort
from mote.runtime.models.output_snapshots import bind_output_snapshot_accumulator

from .conftest import FakeLLM, FakeMemory, FakeReporter, history_with_calls, make_tool_response


def make_engine(*, memory=None, config=None) -> InferenceEngine:
    return InferenceEngine(
        memory=memory or FakeMemory(),
        config=config or {},
        inference_port=RuntimeModelInferencePort(),
        snapshot_scope=bind_output_snapshot_accumulator,
        output_observer=lambda _event: None,
    )


class TestConstruction:
    def test_initial_state(self):
        mem = FakeMemory()
        cfg = {"x": 1}
        engine = InferenceEngine(
            memory=mem,
            config=cfg,
            inference_port=RuntimeModelInferencePort(),
            snapshot_scope=bind_output_snapshot_accumulator,
            output_observer=lambda _event: None,
        )
        assert engine.target is None
        assert engine.memory is mem
        assert engine.config is cfg
        assert isinstance(engine.result, InferenceResult)
        assert engine.result.content == ""
        assert engine.result.tool_calls is None

    def test_done_true_before_any_start(self):
        # No task pending -> done.
        assert make_engine().done is True

    def test_is_basethinkengine(self):
        from mote.kernel.inference.base import BaseInferenceEngine

        assert isinstance(make_engine(), BaseInferenceEngine)


class TestXMLChannel:
    @pytest.mark.asyncio
    async def test_runs_and_produces_text_result(self):
        llm = FakeLLM(reply="my thought")
        engine = make_engine()
        await engine.start(
            req=[UserMessage("hi")],
            system_prompt="sys",
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        assert engine.result.content == "my thought"
        # XML channel -> no structured tool calls.
        assert engine.result.tool_calls is None
        assert engine.result.is_native is False

    @pytest.mark.asyncio
    async def test_uses_aask_not_aask_tool(self):
        llm = FakeLLM(reply="t")
        engine = make_engine()
        await engine.start(
            req=[UserMessage("REQ")],
            system_prompt="SYS",
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        assert len(llm.aask_calls) == 1
        assert llm.aask_calls[0]["msg"] == "REQ"
        assert llm.aask_calls[0]["system_msgs"] == ["SYS"]
        assert llm.aask_tool_calls == []

    @pytest.mark.asyncio
    async def test_start_assigns_model_route(self):
        llm = FakeLLM()
        engine = make_engine()
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        assert engine.target.route_id == llm.route.route_id
        await engine.join()

    @pytest.mark.asyncio
    async def test_no_duplicate_passes_through(self):
        # History has unrelated content -> dedup leaves the response untouched.
        mem = FakeMemory([UserMessage(content="something else")])
        llm = FakeLLM(reply="fresh thought")
        engine = make_engine(memory=mem)
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        assert engine.result.content == "fresh thought"


class TestNativeChannel:
    @pytest.mark.asyncio
    async def test_native_schema_is_forwarded_without_removing_ordinary_tools(self):
        from mote.contracts.output import OutputBinding, OutputBindingKind

        llm = FakeLLM(tool_response=make_tool_response(content='{"count": 7}'))
        engine = make_engine()
        specs = (CanonicalToolDefinition(name="Read"),)
        schema = {"type": "object"}

        await engine.start(
            req=[UserMessage("REQ")],
            system_prompt="SYS",
            tool_specs=specs,
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
            output_binding=OutputBinding(OutputBindingKind.NATIVE_SCHEMA),
            output_schema=schema,
        )
        await engine.join()

        assert [tool["name"] for tool in llm.aask_tool_calls[0]["tools"]] == ["Read"]
        assert llm.aask_tool_calls[0]["kwargs"]["output_schema"] == schema

    @pytest.mark.asyncio
    async def test_maps_tool_call_fields(self):
        resp = make_tool_response(("call-1", "Read", {"path": "a.py"}), content="reading")
        llm = FakeLLM(tool_response=resp)
        engine = make_engine()
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            tool_specs=(CanonicalToolDefinition(name="Read"),),
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        assert engine.result.content == "reading"
        assert engine.result.is_native is True
        assert engine.result.tool_calls is not None
        assert engine.result.tool_calls[0].id == "call-1"
        assert engine.result.tool_calls[0].name == "Read"
        assert engine.result.tool_calls[0].arguments == {"path": "a.py"}

    @pytest.mark.asyncio
    async def test_uses_aask_tool_with_specs(self):
        llm = FakeLLM(tool_response=make_tool_response(("1", "Glob", {})))
        engine = make_engine()
        specs = (CanonicalToolDefinition(name="Glob"),)
        await engine.start(
            req=[UserMessage("REQ")],
            system_prompt="SYS",
            tool_specs=specs,
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        assert len(llm.aask_tool_calls) == 1
        assert [tool["name"] for tool in llm.aask_tool_calls[0]["tools"]] == ["Glob"]
        assert llm.aask_tool_calls[0]["system_msgs"] == ["SYS"]
        assert llm.aask_calls == []

    @pytest.mark.asyncio
    async def test_empty_content_becomes_empty_string(self):
        # Empty canonical content remains valid when native tool calls are present.
        llm = FakeLLM(tool_response=make_tool_response(("1", "Bash", {"cmd": "ls"})))
        engine = make_engine()
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            tool_specs=(CanonicalToolDefinition(name="Bash"),),
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        assert engine.result.content == ""
        assert engine.result.is_native is True

    @pytest.mark.asyncio
    async def test_no_tool_calls_yields_empty_list(self):
        # Native channel with zero calls -> tool_calls == [] (still is_native).
        llm = FakeLLM(tool_response=make_tool_response(content="just text"))
        engine = make_engine()
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            tool_specs=(CanonicalToolDefinition(name="X"),),
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        assert engine.result.tool_calls == ()
        assert engine.result.is_native is True

    @pytest.mark.asyncio
    async def test_unique_calls_not_overridden(self):
        # History signatures don't match -> keep the original calls.
        mem = FakeMemory(history_with_calls([{"name": "Read", "args": {"path": "z"}}]))
        llm = FakeLLM(tool_response=make_tool_response(("1", "Read", {"path": "a"})))
        engine = make_engine(memory=mem)
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            tool_specs=(CanonicalToolDefinition(name="Read"),),
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        assert engine.result.tool_calls is not None
        assert engine.result.tool_calls[0].name == "Read"
        assert engine.result.tool_calls[0].arguments == {"path": "a"}

    @pytest.mark.asyncio
    async def test_sig_hist_ignores_messages_without_tool_calls(self):
        # Plain messages (no TOOL_CALLS metadata) must not count toward repeats.
        sig = [{"name": "Editor", "args": {"path": "a"}}]
        mem = FakeMemory([UserMessage(content="noise")] * 5 + history_with_calls(sig, sig))
        llm = FakeLLM(tool_response=make_tool_response(("1", "Editor", {"path": "a"})))
        engine = make_engine(memory=mem)
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            tool_specs=(CanonicalToolDefinition(name="Editor"),),
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        # Only 2 matching signatures (< 3) -> no override.
        assert engine.result.tool_calls is not None
        assert engine.result.tool_calls[0].name == "Editor"


class TestTaskLifecycle:
    @pytest.mark.asyncio
    async def test_done_false_after_start_true_after_join(self):
        # start schedules the task but yields control before it runs.
        llm = FakeLLM()
        engine = make_engine()
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        assert engine.done is False
        await engine.join()
        assert engine.done is True

    @pytest.mark.asyncio
    async def test_join_clears_task(self):
        engine = make_engine()
        llm = FakeLLM()
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
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
        first_llm = FakeLLM(reply="one")
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            target=engine._inference_port.pin_route(first_llm.route),
            model_call_id="call-1",
        )
        await engine.join()
        first = engine.result
        second_llm = FakeLLM(reply="two")
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            target=engine._inference_port.pin_route(second_llm.route),
            model_call_id="call-2",
        )
        await engine.join()
        assert engine.result is not first
        assert engine.result.content == "two"


class TestReporter:
    @pytest.mark.asyncio
    async def test_emits_react_report(self):
        engine = make_engine()
        llm = FakeLLM()
        await engine.start(
            req=[UserMessage("r")],
            system_prompt="s",
            target=engine._inference_port.pin_route(llm.route),
            model_call_id="call",
        )
        await engine.join()
        assert FakeReporter.instances, "ThoughtReporter should be entered"
        reporter = FakeReporter.instances[0]
        assert reporter.kwargs.get("enable_llm_stream") is True
        assert ({"type": "react"}, "object") in reporter.reports
