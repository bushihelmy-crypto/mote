#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for OpenAILLM's native tool-use streaming (``_achat_completion_stream_tool``).

No network: ``llm.aclient`` is swapped for a fake whose
``chat.completions.create(stream=True)`` yields hand-built ``ChatCompletionChunk``
look-alikes (SimpleNamespace). Covers text-delta streaming, tool-call fragment
accumulation across chunks, response reassembly into a ``ChatCompletion``, and the
``aask_tool(stream=True)`` integration.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mote.contracts.config.model.llm import LLMConfig
from mote.product.models.providers.openai_chat import OpenAILLM
from mote.runtime.events import LLMStreamDeltaEvent, bind_telemetry
from mote.runtime.models.cost import CostTracker
from mote.ztest.telemetry import InlineTelemetry


# -- fakes ------------------------------------------------------------------
def _delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _tc(index, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=id, type="function", function=SimpleNamespace(name=name, arguments=arguments)
    )


def _choice(delta, finish_reason=None):
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def _chunk(choices, usage=None):
    return SimpleNamespace(choices=choices, usage=usage)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeStream(self._chunks)


class _FakeClient:
    def __init__(self, chunks):
        self.chat = SimpleNamespace(completions=_FakeCompletions(chunks))


def _make_llm():
    cfg = LLMConfig(
        api_type="openai", base_url="https://api.openai.com/v1", model="gpt-4o", api_key="sk-x", max_token=512
    )
    llm = OpenAILLM(cfg)
    llm.cost_manager = CostTracker()
    return llm


def run(coro):
    return asyncio.run(coro)


class _StreamCapture:
    """Telemetry handler that collects streamed LLM tokens."""

    def __init__(self):
        self.tokens: list[str] = []

    def handle_sync(self, event) -> None:
        if isinstance(event, LLMStreamDeltaEvent):
            self.tokens.append(event.token)

    async def handle(self, event):
        return None


def _capture_stream(fn):
    """Run *fn* while capturing tokens emitted through bound telemetry."""
    cap = _StreamCapture()
    telemetry = InlineTelemetry(cap)
    with bind_telemetry(telemetry):
        result = fn()
    return result, "".join(cap.tokens)


# -- tests ------------------------------------------------------------------
def test_stream_tool_text_and_tool_calls():
    """Text deltas stream live; tool-call fragments reassemble across chunks."""
    llm = _make_llm()
    chunks = [
        _chunk([_choice(_delta(content="Hello "))]),
        _chunk([_choice(_delta(content="world"))]),
        _chunk([_choice(_delta(tool_calls=[_tc(0, id="call_a", name="Read", arguments='{"file_path":')]))]),
        _chunk([_choice(_delta(tool_calls=[_tc(0, arguments='"a.py"}')]), finish_reason="tool_calls")]),
    ]
    llm.aclient = _FakeClient(chunks)

    rsp, streamed = _capture_stream(
        lambda: run(llm._achat_completion_stream_tool([{"role": "user", "content": "hi"}], raise_if_empty=False))
    )

    assert streamed == "Hello world\n"  # mirrored deltas + trailing newline
    assert llm.get_choice_text(rsp) == "Hello world"
    assert llm.get_choice_tool_calls(rsp) == [{"id": "call_a", "name": "Read", "arguments": {"file_path": "a.py"}}]


def test_stream_tool_text_only():
    """A plain text reply (no tool calls) streams and reassembles cleanly."""
    llm = _make_llm()
    chunks = [
        _chunk([_choice(_delta(content="the "))]),
        _chunk([_choice(_delta(content="answer"), finish_reason="stop")]),
    ]
    llm.aclient = _FakeClient(chunks)

    rsp, streamed = _capture_stream(
        lambda: run(llm._achat_completion_stream_tool([{"role": "user", "content": "hi"}], raise_if_empty=False))
    )

    assert streamed == "the answer\n"
    assert llm.get_choice_text(rsp) == "the answer"
    assert llm.get_choice_tool_calls(rsp) == []


def test_stream_tool_parallel_calls():
    """Two tool calls at distinct indices accumulate independently, ordered."""
    llm = _make_llm()
    chunks = [
        _chunk(
            [
                _choice(
                    _delta(
                        tool_calls=[
                            _tc(0, id="c0", name="Read", arguments='{"p":"x"}'),
                            _tc(1, id="c1", name="Bash", arguments='{"command":"ls"}'),
                        ]
                    ),
                    finish_reason="tool_calls",
                )
            ]
        ),
    ]
    llm.aclient = _FakeClient(chunks)

    rsp = run(llm._achat_completion_stream_tool([{"role": "user", "content": "hi"}], raise_if_empty=False))
    calls = llm.get_choice_tool_calls(rsp)
    assert [c["name"] for c in calls] == ["Read", "Bash"]
    assert calls[0]["arguments"] == {"p": "x"}
    assert calls[1]["arguments"] == {"command": "ls"}


def test_aask_tool_uses_stream_path():
    """aask_tool(stream=True, the default) routes through the streaming completion."""
    llm = _make_llm()
    chunks = [
        _chunk(
            [
                _choice(
                    _delta(tool_calls=[_tc(0, id="c9", name="Glob", arguments='{"pattern":"*.py"}')]),
                    finish_reason="tool_calls",
                )
            ]
        ),
    ]
    llm.aclient = _FakeClient(chunks)

    rsp = run(llm.aask_tool("find files", tools=[{"type": "function", "function": {"name": "Glob", "parameters": {}}}]))
    assert rsp.content == ""
    assert [c.name for c in rsp.tool_calls] == ["Glob"]
    assert rsp.tool_calls[0].arguments == {"pattern": "*.py"}
    # The fake recorded a streaming create call.
    assert llm.aclient.chat.completions.last_kwargs.get("stream") is True


def test_aask_tool_native_schema_keeps_tools_and_adds_response_format():
    llm = _make_llm()
    llm.aclient = _FakeClient([_chunk([_choice(_delta(content='{"count":7}'), finish_reason="stop")])])
    tools = [{"type": "function", "function": {"name": "Read", "parameters": {}}}]
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }

    rsp = run(llm.aask_tool("finish", tools=tools, output_schema=schema))

    kwargs = llm.aclient.chat.completions.last_kwargs
    assert rsp.content == '{"count":7}'
    assert kwargs["tools"] is tools
    assert kwargs["response_format"]["type"] == "json_schema"
    wire_schema = kwargs["response_format"]["json_schema"]["schema"]
    assert wire_schema["properties"] == schema["properties"]
    assert wire_schema["required"] == ["count"]
    assert wire_schema["additionalProperties"] is False
