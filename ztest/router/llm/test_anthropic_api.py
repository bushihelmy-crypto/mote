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

from mote.contracts.config.model.llm import LLMConfig, LLMType
from mote.contracts.events.model import LLMStreamDeltaEvent
from mote.contracts.model.transport import resolve_api_type
from mote.product.models.bootstrap import builtin_provider_registry
from mote.product.models.providers.anthropic import AnthropicLLM
from mote.runtime.events import bind_telemetry
from mote.runtime.models.cost import CostTracker
from mote.ztest.telemetry import InlineTelemetry


def create_llm_instance(config):
    return builtin_provider_registry().create(config)


# -- fakes ------------------------------------------------------------------
class _StreamCapture:
    """Telemetry handler that collects streamed LLM tokens."""

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


def _strip_cache(obj):
    """Deep-copy ``obj`` (message list / tool list / block) sans cache_control.

    Lets conversion assertions ignore the prompt-cache markers that
    ``_apply_cache_breakpoints`` sprinkles on the last message/tool blocks.
    """
    if isinstance(obj, dict):
        return {k: _strip_cache(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip_cache(v) for v in obj]
    return obj


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
        _, conv = llm._convert_messages([{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""}])
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


# -- prompt caching (Anthropic manual cache_control) ------------------------
_EPHEMERAL = {"type": "ephemeral"}


class TestPromptCache:
    def test_system_string_becomes_block_with_marker(self):
        llm = _make_llm()
        kw = llm._cons_kwargs([{"role": "system", "content": "guide"}, {"role": "user", "content": "hi"}])
        # The plain system string is normalized into a single marked text block.
        assert kw["system"] == [{"type": "text", "text": "guide", "cache_control": _EPHEMERAL}]

    def test_single_message_block_marked(self):
        # With only one message (empty history) there is no stable prefix to cache
        # separately, so the marker falls back to the last (only) message's block.
        llm = _make_llm()
        kw = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        last_block = kw["messages"][-1]["content"][-1]
        assert last_block["cache_control"] == _EPHEMERAL

    def test_marker_skips_ephemeral_tail_anchors_last_durable(self):
        # mote assembles the request as ``stored_history + [ephemeral_tail]``. The
        # tail declares CACHE_INTENT_EPHEMERAL_TAIL (surfaced on the wire as the
        # private ``_cache_intent`` key). The single message-level marker must skip
        # that tagged block and anchor on the last DURABLE block — the true end of
        # the cacheable prefix — never the volatile tail (which changes every turn).
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three", "_cache_intent": "ephemeral_tail"},
            ]
        )
        markers = [
            block
            for msg in kw["messages"]
            for block in msg["content"]
            if isinstance(block, dict) and "cache_control" in block
        ]
        # Exactly one message-level marker, on the assistant "two" (last durable).
        assert len(markers) == 1
        assert kw["messages"][-2]["content"][-1]["cache_control"] == _EPHEMERAL
        # The ephemeral tail is NOT marked...
        assert "cache_control" not in kw["messages"][-1]["content"][-1]
        # ...and the private routing key never reaches the wire.
        for msg in kw["messages"]:
            for block in msg["content"]:
                assert "_cache_intent" not in block

    def test_tail_merged_into_tool_result_turn_still_skipped(self):
        # Native tool-use: the ephemeral tail (user text) is MERGED by
        # ``_append_blocks`` into the same user turn that holds the tool_result
        # block. A positional [-2] heuristic would strand the tool_result outside
        # the cached prefix; block-level intent anchors on the tool_result instead.
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [
                {"role": "user", "content": "start"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "result-body"},
                {"role": "user", "content": "reminder", "_cache_intent": "ephemeral_tail"},
            ]
        )
        # The tool_result and the ephemeral tail share the final user turn.
        last_turn = kw["messages"][-1]["content"]
        tool_block = next(b for b in last_turn if b.get("type") == "tool_result")
        tail_block = next(b for b in last_turn if b.get("type") == "text")
        # Marker anchors on the durable tool_result, not the ephemeral tail text.
        assert tool_block["cache_control"] == _EPHEMERAL
        assert "cache_control" not in tail_block
        # No leaked routing key.
        for block in last_turn:
            assert "_cache_intent" not in block

    def test_last_tool_marked(self):
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [{"role": "user", "content": "hi"}],
            tools=[
                {"name": "A", "input_schema": {"type": "object"}},
                {"name": "B", "input_schema": {"type": "object"}},
            ],
        )
        assert "cache_control" not in kw["tools"][0]
        assert kw["tools"][-1]["cache_control"] == _EPHEMERAL

    def test_at_most_three_breakpoints(self):
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [{"role": "system", "content": "guide"}, {"role": "user", "content": "hi"}],
            tools=[{"name": "A", "input_schema": {"type": "object"}}],
        )
        count = 0
        for block in kw["system"]:
            count += "cache_control" in block
        for tool in kw["tools"]:
            count += "cache_control" in tool
        for msg in kw["messages"]:
            for block in msg["content"]:
                count += isinstance(block, dict) and "cache_control" in block
        # Anthropic allows at most 4; we place exactly 3 (system / tools / tail).
        assert count == 3

    def test_disabled_places_no_markers(self):
        llm = _make_llm(use_prompt_cache=False)
        kw = llm._cons_kwargs(
            [{"role": "system", "content": "guide"}, {"role": "user", "content": "hi"}],
            tools=[{"name": "A", "input_schema": {"type": "object"}}],
        )
        assert kw["system"] == "guide"  # left as a plain string, untouched
        for msg in kw["messages"]:
            for block in msg["content"]:
                assert "cache_control" not in block
        for tool in kw["tools"]:
            assert "cache_control" not in tool


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
        # Prompt-cache breakpoints ride the last message/tool block; ignore them
        # here (covered by TestPromptCache) by stripping cache_control.
        assert _strip_cache(kw["messages"]) == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        assert _strip_cache(kw["tools"]) == [{"name": "A", "input_schema": {"type": "object"}}]
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
        from mote.contracts.model.provider_errors import LLMEmptyResponseError

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
        from mote.contracts.model.provider_errors import LLMRateLimitError

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

        cap = _StreamCapture()
        telemetry = InlineTelemetry(cap)
        with bind_telemetry(telemetry):
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
        from mote.product.models.providers.openai_chat import OpenAILLM

        cfg = LLMConfig(
            api_type="openai", base_url="https://openrouter.ai/api/v1", model="claude-3-5-sonnet", api_key="x"
        )
        assert resolve_api_type(cfg) == LLMType.OPENAI
        assert isinstance(create_llm_instance(cfg), OpenAILLM)

    def test_explicit_anthropic_api_type(self):
        cfg = LLMConfig(api_type="anthropic", base_url="https://api.openai.com/v1", model="claude", api_key="x")
        assert resolve_api_type(cfg) == LLMType.ANTHROPIC

    def test_anthropic_suffix_endpoint_selects_native(self):
        # Chinese vendors expose an Anthropic-compatible surface at a /anthropic
        # path (MiniMax, Kimi-coding) alongside their OpenAI /v1 surface.
        for url in (
            "https://api.minimax.io/anthropic",
            "https://api.minimaxi.com/anthropic",
            "https://api.moonshot.cn/anthropic/",  # trailing slash tolerated
        ):
            cfg = LLMConfig(api_type="openai", base_url=url, model="MiniMax-M2", api_key="x")
            assert resolve_api_type(cfg) == LLMType.ANTHROPIC, url
            assert isinstance(create_llm_instance(cfg), AnthropicLLM)

    def test_openai_v1_surface_of_same_vendor_stays_openai(self):
        from mote.product.models.providers.openai_chat import OpenAILLM

        # The vendor's OpenAI-compatible /v1 surface must NOT match the
        # /anthropic detector — only the explicit anthropic surface takes native.
        cfg = LLMConfig(api_type="openai", base_url="https://api.minimaxi.com/v1", model="MiniMax-M2", api_key="x")
        assert resolve_api_type(cfg) == LLMType.OPENAI
        assert isinstance(create_llm_instance(cfg), OpenAILLM)


# -- reasoning effort (unified thinking) ------------------------------------
class TestReasoningEffort:
    def test_effort_enables_thinking_and_drops_temperature(self):
        llm = _make_llm(reasoning_effort="high", temperature=0.7)  # opus-4 supports thinking
        kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 16384}
        # The API forbids temperature alongside thinking → dropped even if set.
        assert "temperature" not in kwargs

    def test_effort_budget_table(self):
        for effort, budget in [("minimal", 1024), ("low", 4096), ("medium", 8192), ("high", 16384)]:
            llm = _make_llm(reasoning_effort=effort)
            kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
            assert kwargs["thinking"]["budget_tokens"] == budget

    def test_no_effort_no_thinking(self):
        llm = _make_llm(temperature=0.5)
        kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert "thinking" not in kwargs
        assert kwargs["temperature"] == 0.5

    def test_incapable_model_ignores_effort(self):
        llm = _make_llm(reasoning_effort="high")
        llm.model = "claude-2"  # not a thinking model
        kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert "thinking" not in kwargs

    def test_thinking_grows_max_tokens_for_answer_headroom(self):
        # config max_token=2048 < high budget (16384): the API requires
        # max_tokens > budget_tokens, so the envelope grows to budget + answer
        # floor rather than 400-ing (and never shrinks the requested budget).
        llm = _make_llm(reasoning_effort="high")  # budget 16384, config max_token=2048
        kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert kwargs["thinking"]["budget_tokens"] == 16384
        assert kwargs["max_tokens"] == 16384 + 4096  # budget + _ANSWER_TOKEN_FLOOR
        assert kwargs["max_tokens"] > kwargs["thinking"]["budget_tokens"]

    def test_thinking_keeps_large_configured_max_tokens(self):
        # A configured ceiling already roomy enough is preserved (never shrunk).
        cfg = LLMConfig(
            api_type="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-opus-4-8",
            api_key="sk-test",
            max_token=100_000,
            reasoning_effort="low",  # budget 4096
        )
        llm = AnthropicLLM(cfg)
        llm.cost_manager = CostTracker()
        kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert kwargs["max_tokens"] == 100_000
        assert kwargs["max_tokens"] > kwargs["thinking"]["budget_tokens"]

    def test_no_thinking_leaves_max_tokens_untouched(self):
        # Without thinking the envelope is the plain configured ceiling.
        llm = _make_llm()  # max_token=2048, no effort
        kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert kwargs["max_tokens"] == 2048


# -- error classification (handlers) ---------------------------------------
class TestErrorClassification:
    def test_anthropic_status_errors_mapped(self):
        import anthropic
        import httpx

        from mote.contracts.model.provider_errors import LLMAuthenticationError, LLMBadRequestError
        from mote.runtime.resilience.error_classification import classify_llm_error, is_retryable

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

        auth = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
        assert isinstance(classify_llm_error(auth), LLMAuthenticationError)

        bad = anthropic.BadRequestError("bad", response=httpx.Response(400, request=request), body=None)
        assert isinstance(classify_llm_error(bad), LLMBadRequestError)

        overloaded = anthropic.InternalServerError("boom", response=httpx.Response(500, request=request), body=None)
        # InternalServerError is in the transient allowlist.
        assert is_retryable(overloaded) is True

    def test_retry_after_header_stamped_onto_typed_error(self):
        import anthropic
        import httpx

        from mote.contracts.model.provider_errors import LLMRateLimitError
        from mote.runtime.resilience.error_classification import classify_llm_error

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(429, request=request, headers={"retry-after": "17"})
        rate = anthropic.RateLimitError("slow down", response=response, body=None)

        err = classify_llm_error(rate)
        assert isinstance(err, LLMRateLimitError)
        assert err.retry_after == 17.0
        assert err.context.get("retry_after") == 17.0

    def test_missing_retry_after_header_leaves_none(self):
        import anthropic
        import httpx

        from mote.contracts.model.provider_errors import LLMRateLimitError
        from mote.runtime.resilience.error_classification import classify_llm_error

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(429, request=request)  # no Retry-After header
        rate = anthropic.RateLimitError("slow down", response=response, body=None)

        err = classify_llm_error(rate)
        assert isinstance(err, LLMRateLimitError)
        assert err.retry_after is None

    def test_future_http_date_retry_after_parsed_to_delta(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        import anthropic
        import httpx

        from mote.runtime.resilience.error_classification import classify_llm_error

        # RFC 7231 second form: an absolute HTTP-date → converted to a positive
        # delay by subtracting *now* (aware UTC). Use a far-future instant so the
        # delta stays comfortably positive regardless of test-run latency.
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(429, request=request, headers={"retry-after": format_datetime(future)})
        rate = anthropic.RateLimitError("slow down", response=response, body=None)

        err = classify_llm_error(rate)
        assert err.retry_after is not None
        # ~120s minus the tiny elapsed since we computed ``future``.
        assert 100.0 < err.retry_after <= 120.0

    def test_past_http_date_retry_after_ignored(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        import anthropic
        import httpx

        from mote.runtime.resilience.error_classification import classify_llm_error

        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(429, request=request, headers={"retry-after": format_datetime(past)})
        rate = anthropic.RateLimitError("slow down", response=response, body=None)

        err = classify_llm_error(rate)
        # A date already in the past → non-positive delta → None (normal backoff).
        assert getattr(err, "retry_after", None) is None

    def test_garbage_retry_after_header_ignored(self):
        import anthropic
        import httpx

        from mote.runtime.resilience.error_classification import classify_llm_error

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(429, request=request, headers={"retry-after": "not-a-date-or-number"})
        rate = anthropic.RateLimitError("slow down", response=response, body=None)

        err = classify_llm_error(rate)
        assert getattr(err, "retry_after", None) is None


class TestDescribeImage:
    """``adescribe_image`` (base-class seam behind WebBrowser's read_image).

    Gated ONLY on model vision capability (``support_image_input`` →
    ``supports_vision``): a non-vision model raises ``NotImplementedError``
    BEFORE any network call so the tool degrades cleanly; a vision model issues
    a single multimodal ``aask`` carrying the image.
    """

    def test_incapable_model_raises_notimplemented(self):
        llm = _make_llm()
        llm.model = "claude-2"  # not multimodal
        with pytest.raises(NotImplementedError):
            run(llm.adescribe_image("aGVsbG8="))

    def test_capable_model_feeds_image_to_aask(self):
        llm = _make_llm()  # claude-opus-4-8 → multimodal
        seen: dict = {}

        async def _fake_aask(msg, *, images=None, stream=True, timeout=None, **kw):
            seen["msg"] = msg
            seen["images"] = images
            seen["stream"] = stream
            return "a cat on a mat"

        llm.aask = _fake_aask  # type: ignore[method-assign]
        out = run(llm.adescribe_image("aW1n", prompt="what is this?"))
        assert out == "a cat on a mat"
        # The image rides the multimodal images= param; the prompt steers it.
        assert seen["images"] == ["aW1n"]
        assert seen["msg"] == "what is this?"
        # Isolated one-shot: never streamed.
        assert seen["stream"] is False

    def test_empty_prompt_uses_default_ask(self):
        llm = _make_llm()
        seen: dict = {}

        async def _fake_aask(msg, *, images=None, stream=True, timeout=None, **kw):
            seen["msg"] = msg
            return "desc"

        llm.aask = _fake_aask  # type: ignore[method-assign]
        run(llm.adescribe_image("aW1n"))
        assert "Describe this image" in seen["msg"]


class TestUnreadableMediaNotice:
    """A non-vision model must be TOLD its attachments were withheld.

    ``_user_msg`` drops media on a model whose ``support_image_input`` is
    False; instead of silently returning bare text (which leaves the model
    reading e.g. "Shown below." with nothing below), it appends an honest
    notice so the model does not hallucinate having seen an image.
    """

    def test_non_vision_model_gets_notice_not_silent_drop(self):
        llm = _make_llm()
        llm.model = "claude-2"  # not multimodal
        out = llm._user_msg("look at this", images=["aW1n"])
        assert out["role"] == "user"
        assert out["content"].startswith("look at this")
        assert "cannot read images" in out["content"]
        assert "not shown" in out["content"]

    def test_notice_counts_images_and_pdfs(self):
        llm = _make_llm()
        llm.model = "claude-2"
        out = llm._user_msg("hi", images=["a", "b"], pdfs=["p"])
        assert "2 images" in out["content"]
        assert "1 PDF" in out["content"]

    def test_vision_model_still_attaches_media(self):
        llm = _make_llm()  # claude-opus-4-8 → multimodal
        out = llm._user_msg("see", images=["aW1n"])
        # Real multimodal content list, no notice.
        assert isinstance(out["content"], list)
        assert any(b.get("type") == "image_url" for b in out["content"])

    def test_no_media_is_plain_text_unchanged(self):
        llm = _make_llm()
        llm.model = "claude-2"
        out = llm._user_msg("just text")
        assert out == {"role": "user", "content": "just text"}
