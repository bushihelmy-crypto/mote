#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the LLM request/response/error events emitted at the call chokepoint.

``BaseLLM._run_with_recovery`` is the single LLM-call seam. It now opens a
request/response (or request/error) pair on the active event bus per recovery
attempt. These tests drive the seam directly with a fake ``send`` (no network),
bind a capture subscriber, and assert the emitted events carry the model,
provider, per-call token usage and USD cost.
"""
from __future__ import annotations

import asyncio

from metagpt.common.config.config.llm_config import LLMConfig
from metagpt.common.events import (
    EventBus,
    LLMErrorEvent,
    LLMRequestEvent,
    LLMResponseEvent,
    set_bus,
    span,
)
from metagpt.common.logs import bind_trace
from metagpt.router.cost import CostTracker
from metagpt.router.cost.usage import TokenUsage
from metagpt.router.llm.llm_response import LLMResponse, LLMToolCall
from metagpt.router.llm.openai_api import OpenAILLM


def run(coro):
    return asyncio.run(coro)


def _make_llm() -> OpenAILLM:
    cfg = LLMConfig(api_type="openai", base_url="https://api.openai.com/v1", model="gpt-4o", api_key="sk-x", max_token=512)
    llm = OpenAILLM(cfg)
    llm.cost_manager = CostTracker()
    return llm


class _Capture:
    priority = 50

    def __init__(self):
        self.requests: list = []
        self.responses: list = []
        self.errors: list = []

    async def handle(self, event):
        if isinstance(event, LLMRequestEvent):
            self.requests.append(event)
        elif isinstance(event, LLMResponseEvent):
            self.responses.append(event)
        elif isinstance(event, LLMErrorEvent):
            self.errors.append(event)
        return None


def _with_bus(fn):
    bus = EventBus()
    cap = _Capture()
    bus.subscribe(cap)
    with set_bus(bus):
        result = fn()
    return result, cap


# -- tests ------------------------------------------------------------------
def test_successful_call_emits_request_and_response_with_usage_and_cost():
    llm = _make_llm()

    async def _send(active, messages):
        # Simulate the provider recording usage as a real completion would.
        active.cost_manager.add(TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120), active.model)
        return LLMResponse(content="hello", tool_calls=[LLMToolCall(id="c1", name="Read", arguments={"p": "a"})])

    (rsp, cap) = _with_bus(lambda: run(llm._run_with_recovery(_send, [{"role": "user", "content": "hi"}])))

    assert isinstance(rsp, LLMResponse)
    assert len(cap.requests) == 1 and len(cap.responses) == 1 and cap.errors == []

    req = cap.requests[0]
    assert req.model == "gpt-4o"
    assert req.provider == "openai"
    assert req.messages == [{"role": "user", "content": "hi"}]
    # No ambient span / trace bound -> the linkage fields stay blank.
    assert req.parent_span_id is None
    assert req.trace_id == ""

    res = cap.responses[0]
    assert res.request_id == req.request_id  # correlated pair
    assert res.content == "hello"
    assert res.tool_calls == [{"id": "c1", "name": "Read", "arguments": {"p": "a"}}]
    assert res.usage["input_tokens"] == 100 and res.usage["output_tokens"] == 20
    assert res.cost_usd > 0.0
    assert res.latency_ms >= 0.0


def test_request_stamps_parent_span_and_trace_id_inside_a_span():
    llm = _make_llm()

    async def _send(active, messages):
        return LLMResponse(content="ok")

    async def _go():
        bus = EventBus()
        cap = _Capture()
        bus.subscribe(cap)
        with bind_trace("sess-9"), set_bus(bus):
            async with span("act") as sid:
                await llm._run_with_recovery(_send, [{"role": "user", "content": "hi"}])
                return cap, sid

    cap, sid = run(_go())
    req = cap.requests[0]
    assert req.parent_span_id == sid
    assert req.trace_id == "sess-9"


def test_text_result_carries_content_string():
    llm = _make_llm()

    async def _send(active, messages):
        active.cost_manager.add(TokenUsage(input_tokens=5, output_tokens=3, total_tokens=8), active.model)
        return "plain text answer"

    (_rsp, cap) = _with_bus(lambda: run(llm._run_with_recovery(_send, [{"role": "user", "content": "q"}])))
    assert cap.responses[0].content == "plain text answer"


def test_failed_call_emits_error_then_reraises():
    llm = _make_llm()

    async def _send(active, messages):
        raise ValueError("boom")

    import pytest

    def _go():
        return run(llm._run_with_recovery(_send, [{"role": "user", "content": "hi"}]))

    bus = EventBus()
    cap = _Capture()
    bus.subscribe(cap)
    with set_bus(bus):
        with pytest.raises(ValueError):
            _go()

    assert len(cap.requests) == 1
    assert cap.responses == []
    assert len(cap.errors) == 1
    err = cap.errors[0]
    assert err.request_id == cap.requests[0].request_id
    assert err.error_type == "ValueError"
    assert "boom" in err.error


def test_no_bus_bound_is_noop():
    """Without a bound bus the seam still works (events are dropped silently)."""
    llm = _make_llm()

    async def _send(active, messages):
        return "ok"

    rsp = run(llm._run_with_recovery(_send, [{"role": "user", "content": "hi"}]))
    assert rsp == "ok"
