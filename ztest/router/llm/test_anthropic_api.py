#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the native Anthropic Messages API provider (AnthropicLLM).

No network is touched: ``llm.aclient`` is swapped for a fake client whose
``messages.create`` / ``messages.stream`` return hand-built response objects.
Covers OpenAI<->Anthropic message conversion, tool spec / tool_choice mapping,
response normalization, cost accounting, streaming, error translation, and the
base_url auto-detection in ``create_llm_instance``.
"""
from __future__ import annotations

import asyncio

import pytest

from metagpt.common.config.config.llm_config import LLMConfig, LLMType
from metagpt.common.events import EventBus, LLMStreamDeltaEvent, set_bus
from metagpt.router.cost import CostTracker
from metagpt.router.llm.anthropic_api import AnthropicLLM
from metagpt.router.llm.llm_provider_registry import create_llm_instance, resolve_api_type


# -- fakes ------------------------------------------------------------------
class _StreamCapture:
    """Bus subscriber that collects streamed LLM tokens (sync delivery)."""

    priority = 50

    def __init__(self):
        self.tokens: list[str] = []

    def handle_sync(self, event) -> None:
        if isinstance(event, LLMStreamDeltaEvent):
            self.tokens.append(event.token)

    async def handle(self, event):
        return None


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Usage:
    def __init__(self, input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class _Resp:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage or _Usage()


class _FakeMessages:
    """Stand-in for ``client.messages`` capturing create kwargs / returning canned resp."""

    def __init__(self, resp=None, stream_texts=None, final=None, raise_exc=None):
        self.resp = resp
        self.stream_texts = stream_texts or []
        self.final = final
        self.raise_exc = raise_exc
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.resp

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeStream(self.stream_texts, self.final)


class _FakeStream:
    def __init__(self, texts, final):
        self._texts = texts
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def _gen():
            for t in self._texts:
                yield t

        return _gen()

    async def get_final_message(self):
        return self._final


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages


def _make_llm(**overrides):
    cfg = LLMConfig(
        api_type="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-opus-4-8",
        api_key="sk-test",
        max_token=2048,
        **overrides,
    )
    llm = AnthropicLLM(cfg)
    llm.cost_manager = CostTracker()
    return llm


def run(coro):
    return asyncio.run(coro)


# -- message conversion -----------------------------------------------------
class TestConvertMessages:
    def test_system_extracted_and_joined(self):
        llm = _make_llm()
        system, conv = llm._convert_messages(
            [{"role": "system", "content": "A"}, {"role": "system", "content": "B"}, {"role": "user", "content": "hi"}]
        )
        assert system == "A\n\nB"
        assert conv == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]

    def test_assistant_tool_calls_become_tool_use(self):
        llm = _make_llm()
        _, conv = llm._convert_messages(
            [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": "ok",
                    "tool_calls": [
                        {"id": "t1", "type": "function", "function": {"name": "Bash", "arguments": '{"cmd": "ls"}'}}
                    ],
                },
            ]
        )
        assistant = conv[1]
        assert assistant["role"] == "assistant"
        assert assistant["content"][0] == {"type": "text", "text": "ok"}
        assert assistant["content"][1] == {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}}

    def test_consecutive_tool_results_merge_into_one_user_turn(self):
        llm = _make_llm()
        _, conv = llm._convert_messages(
            [
                {"role": "tool", "tool_call_id": "t1", "content": "r1"},
                {"role": "tool", "tool_call_id": "t2", "content": "r2"},
            ]
        )
        assert len(conv) == 1
        assert conv[0]["role"] == "user"
        assert [b["tool_use_id"] for b in conv[0]["content"]] == ["t1", "t2"]

    def test_image_url_data_uri_to_base64_block(self):
        llm = _make_llm()
        _, conv = llm._convert_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC"}},
                    ],
                }
            ]
        )
        blocks = conv[0]["content"]
        assert blocks[0] == {"type": "text", "text": "look"}
        assert blocks[1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "ABC"}}

    def test_image_url_http_to_url_block(self):
        llm = _make_llm()
        block = llm._image_url_to_block({"url": "https://x/y.png"})
        assert block == {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}

    def test_malformed_tool_arguments_default_empty(self):
        llm = _make_llm()
        _, conv = llm._convert_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "t1", "function": {"name": "X", "arguments": "not json"}}],
                }
            ]
        )
        assert conv[0]["content"][0]["input"] == {}

    def test_empty_assistant_without_tools_is_dropped(self):
        llm = _make_llm()
        _, conv = llm._convert_messages(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""}]
        )
        assert len(conv) == 1 and conv[0]["role"] == "user"


# -- tool spec / tool_choice ------------------------------------------------
class TestToolConversion:
    def test_tool_choice_strings(self):
        llm = _make_llm()
        assert llm._convert_tool_choice("auto") == {"type": "auto"}
        assert llm._convert_tool_choice("required") == {"type": "any"}
        assert llm._convert_tool_choice("any") == {"type": "any"}
        assert llm._convert_tool_choice("none") == {"type": "none"}

    def test_tool_choice_openai_forced(self):
        llm = _make_llm()
        assert llm._convert_tool_choice({"type": "function", "function": {"name": "X"}}) == {
            "type": "tool",
            "name": "X",
        }

    def test_tool_choice_anthropic_passthrough(self):
        llm = _make_llm()
        assert llm._convert_tool_choice({"type": "tool", "name": "Y"}) == {"type": "tool", "name": "Y"}

    def test_convert_tools_openai_to_anthropic(self):
        llm = _make_llm()
        out = llm._convert_tools(
            [{"type": "function", "function": {"name": "A", "description": "d", "parameters": {"type": "object"}}}]
        )
        assert out == [{"name": "A", "description": "d", "input_schema": {"type": "object"}}]

    def test_convert_tools_anthropic_passthrough(self):
        llm = _make_llm()
        spec = {"name": "B", "description": "d", "input_schema": {"type": "object"}}
        assert llm._convert_tools([spec]) == [spec]


# -- response normalization -------------------------------------------------
class TestResponseNormalization:
    def test_get_choice_text_concatenates_text_blocks(self):
        llm = _make_llm()
        rsp = _Resp([_Block(type="text", text="hello "), _Block(type="text", text="world")])
        assert llm.get_choice_text(rsp) == "hello world"

    def test_get_choice_tool_calls(self):
        llm = _make_llm()
        rsp = _Resp(
            [
                _Block(type="text", text="thinking"),
                _Block(type="tool_use", id="t1", name="Bash", input={"cmd": "ls"}),
            ]
        )
        assert llm.get_choice_tool_calls(rsp) == [{"id": "t1", "name": "Bash", "arguments": {"cmd": "ls"}}]

    def test_get_choice_tool_calls_empty_for_text_only(self):
        llm = _make_llm()
        assert llm.get_choice_tool_calls(_Resp([_Block(type="text", text="x")])) == []


# -- completion calls -------------------------------------------------------
class TestCompletion:
    def test_achat_completion_builds_kwargs_and_records_cost(self):
        llm = _make_llm()
        resp = _Resp([_Block(type="text", text="hi there")], usage=_Usage(input_tokens=10, output_tokens=5))
        fake = _FakeMessages(resp=resp)
        llm.aclient = _FakeClient(fake)

        out = run(
            llm._achat_completion(
                [{"role": "user", "content": "hi"}],
                tools=[{"name": "A", "input_schema": {"type": "object"}}],
                tool_choice="auto",
            )
        )
        assert out is resp
        kw = fake.last_kwargs
        assert kw["model"] == "claude-opus-4-8"
        assert kw["max_tokens"] == 2048
        assert kw["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        assert kw["tools"] == [{"name": "A", "input_schema": {"type": "object"}}]
        assert kw["tool_choice"] == {"type": "auto"}
        assert "raise_if_empty" not in kw
        # Cost recorded from the Anthropic usage shape.
        assert llm.cost_manager.total_prompt_tokens == 10
        assert llm.cost_manager.total_completion_tokens == 5

    def test_temperature_omitted_when_not_set(self):
        llm = _make_llm()  # config does not set temperature
        llm.aclient = _FakeClient(_FakeMessages(resp=_Resp([_Block(type="text", text="x")])))
        run(llm._achat_completion([{"role": "user", "content": "hi"}]))
        assert "temperature" not in llm.aclient.messages.last_kwargs

    def test_temperature_sent_when_explicit(self):
        llm = _make_llm(temperature=0.7)
        llm.aclient = _FakeClient(_FakeMessages(resp=_Resp([_Block(type="text", text="x")])))
        run(llm._achat_completion([{"role": "user", "content": "hi"}]))
        assert llm.aclient.messages.last_kwargs["temperature"] == 0.7

    def test_achat_completion_raise_if_empty(self):
        llm = _make_llm()
        fake = _FakeMessages(resp=_Resp([]))
        llm.aclient = _FakeClient(fake)
        from metagpt.common.exception import LLMEmptyResponseError

        with pytest.raises(LLMEmptyResponseError):
            run(llm._achat_completion([{"role": "user", "content": "hi"}], raise_if_empty=True))

    def test_tool_only_response_not_empty(self):
        llm = _make_llm()
        resp = _Resp([_Block(type="tool_use", id="t1", name="A", input={})])
        llm.aclient = _FakeClient(_FakeMessages(resp=resp))
        out = run(llm._achat_completion([{"role": "user", "content": "hi"}], raise_if_empty=True))
        assert out is resp

    def test_acompletion_text_non_stream(self):
        llm = _make_llm()
        llm.aclient = _FakeClient(_FakeMessages(resp=_Resp([_Block(type="text", text="answer")])))
        assert run(llm.acompletion_text([{"role": "user", "content": "hi"}], stream=False)) == "answer"

    def test_stream_concatenates_and_records_cost(self):
        llm = _make_llm()
        final = _Resp([], usage=_Usage(input_tokens=7, output_tokens=3))
        fake = _FakeMessages(stream_texts=["foo", "bar"], final=final)
        llm.aclient = _FakeClient(fake)
        out = run(llm._achat_completion_stream([{"role": "user", "content": "hi"}]))
        assert out == "foobar"
        assert llm.cost_manager.total_prompt_tokens == 7
        assert llm.cost_manager.total_completion_tokens == 3

    def test_create_error_is_classified(self):
        import anthropic
        import httpx

        llm = _make_llm()
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(429, request=request)
        exc = anthropic.RateLimitError("rate limited", response=response, body=None)
        llm.aclient = _FakeClient(_FakeMessages(raise_exc=exc))
        from metagpt.common.exception import LLMRateLimitError

        with pytest.raises(LLMRateLimitError):
            run(llm._achat_completion([{"role": "user", "content": "hi"}]))

    def test_stream_tool_streams_text_and_returns_message(self):
        """The native tool stream mirrors text live and returns the assembled Message."""
        llm = _make_llm()
        final = _Resp(
            [
                _Block(type="text", text="let me read it"),
                _Block(type="tool_use", id="tu_1", name="Read", input={"file_path": "a.py"}),
            ],
            usage=_Usage(input_tokens=5, output_tokens=2),
        )
        fake = _FakeMessages(stream_texts=["let me ", "read it"], final=final)
        llm.aclient = _FakeClient(fake)

        bus = EventBus()
        cap = _StreamCapture()
        bus.subscribe(cap)
        with set_bus(bus):
            rsp = run(llm._achat_completion_stream_tool([{"role": "user", "content": "hi"}], raise_if_empty=False))

        # Text deltas mirrored to the bus as they arrived.
        assert "".join(cap.tokens).startswith("let me read it")
        # Returned object normalizes exactly like the blocking path.
        assert llm.get_choice_text(rsp) == "let me read it"
        calls = llm.get_choice_tool_calls(rsp)
        assert calls == [{"id": "tu_1", "name": "Read", "arguments": {"file_path": "a.py"}}]
        assert llm.cost_manager.total_completion_tokens == 2

    def test_aask_tool_uses_stream_path(self):
        """aask_tool(stream=True) goes through the streaming tool completion."""
        llm = _make_llm()
        final = _Resp([_Block(type="tool_use", id="tu_9", name="Bash", input={"command": "ls"})])
        llm.aclient = _FakeClient(_FakeMessages(stream_texts=[], final=final))
        rsp = run(llm.aask_tool("go", tools=[{"name": "Bash", "input_schema": {}}]))
        assert rsp.content == ""
        assert [c.name for c in rsp.tool_calls] == ["Bash"]
        assert rsp.tool_calls[0].arguments == {"command": "ls"}


# -- auto-detection ---------------------------------------------------------
class TestAutoDetection:
    def test_anthropic_base_url_selects_native(self):
        cfg = LLMConfig(api_type="openai", base_url="https://api.anthropic.com", model="claude-opus-4-8", api_key="x")
        assert resolve_api_type(cfg) == LLMType.ANTHROPIC
        assert isinstance(create_llm_instance(cfg), AnthropicLLM)

    def test_gateway_claude_stays_openai(self):
        from metagpt.router.llm.openai_api import OpenAILLM

        cfg = LLMConfig(
            api_type="openai", base_url="https://openrouter.ai/api/v1", model="claude-3-5-sonnet", api_key="x"
        )
        assert resolve_api_type(cfg) == LLMType.OPENAI
        assert isinstance(create_llm_instance(cfg), OpenAILLM)

    def test_explicit_anthropic_api_type(self):
        cfg = LLMConfig(api_type="anthropic", base_url="https://api.openai.com/v1", model="claude", api_key="x")
        assert resolve_api_type(cfg) == LLMType.ANTHROPIC


# -- error classification (handlers) ---------------------------------------
class TestErrorClassification:
    def test_anthropic_status_errors_mapped(self):
        import anthropic
        import httpx

        from metagpt.common.exception import (
            LLMAuthenticationError,
            LLMBadRequestError,
            classify_llm_error,
        )
        from metagpt.common.exception.handlers import is_retryable

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

        auth = anthropic.AuthenticationError(
            "bad key", response=httpx.Response(401, request=request), body=None
        )
        assert isinstance(classify_llm_error(auth), LLMAuthenticationError)

        bad = anthropic.BadRequestError(
            "bad", response=httpx.Response(400, request=request), body=None
        )
        assert isinstance(classify_llm_error(bad), LLMBadRequestError)

        overloaded = anthropic.InternalServerError(
            "boom", response=httpx.Response(500, request=request), body=None
        )
        # InternalServerError is in the transient allowlist.
        assert is_retryable(overloaded) is True
