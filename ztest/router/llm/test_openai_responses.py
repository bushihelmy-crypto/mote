#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the OpenAI Responses native tool-search (defer_loading) wire path.

``OpenAIResponsesLLM`` is the whole-model takeover for gpt-5.4+: it converts the
framework's OpenAI-shaped message dicts into Responses ``input`` items +
``instructions``, and normalizes the Responses ``output`` back into the agnostic
tool-call contract. Tool Search runs the *custom / client-execution* path: a
SearchTools discovery result (``_tool_references``) becomes a
``tool_search_call`` + ``tool_search_output`` pair whose embedded tool defs carry
``defer_loading:true`` so the API injects them at context end (prefix byte-stable
→ prompt cache preserved). A ``tool_search_call`` in the response is mapped back
to a ``SearchTools`` call so discovery flows through the SAME executor path.

No network: only the pure wire builders (``_convert_messages`` / ``_cons_kwargs``)
and the response normalizers (``get_choice_*``) over hand-built doubles.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mote.contracts.config.model.llm import LLMConfig
from mote.product.models.providers.openai_responses import OpenAIResponsesLLM


def _make_llm(**overrides):
    cfg = LLMConfig(
        api_type="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-5.4",
        api_key="sk-test",  # pragma: allowlist secret
        max_token=2048,
        **overrides,
    )
    return OpenAIResponsesLLM(cfg)


class TestConvertMessages:
    def test_system_becomes_instructions(self):
        llm = _make_llm()
        instructions, items = llm._convert_messages(
            [{"role": "system", "content": "you are helpful"}, {"role": "user", "content": "hi"}]
        )
        assert instructions == "you are helpful"
        # Only the user message becomes an input item.
        assert items == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]

    def test_assistant_text_uses_output_text(self):
        llm = _make_llm()
        _, items = llm._convert_messages([{"role": "assistant", "content": "sure"}])
        assert items == [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "sure"}]}]

    def test_assistant_tool_call_becomes_function_call(self):
        llm = _make_llm()
        _, items = llm._convert_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_1", "function": {"name": "Read", "arguments": {"path": "/x"}}}],
                }
            ]
        )
        assert items == [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "Read",
                "arguments": '{"path": "/x"}',
            }
        ]

    def test_ordinary_tool_result_becomes_function_call_output(self):
        llm = _make_llm()
        _, items = llm._convert_messages([{"role": "tool", "tool_call_id": "call_1", "content": "file body"}])
        assert items == [{"type": "function_call_output", "call_id": "call_1", "output": "file body"}]

    def test_multiple_system_messages_join(self):
        llm = _make_llm()
        instructions, _ = llm._convert_messages(
            [{"role": "system", "content": "a"}, {"role": "system", "content": "b"}]
        )
        assert instructions == "a\n\nb"

    def test_multimodal_content_flattened_to_text(self):
        llm = _make_llm()
        _, items = llm._convert_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look "},
                        {"type": "input_text", "text": "here"},
                    ],
                }
            ]
        )
        assert items[0]["content"][0]["text"] == "look here"

    def test_multimodal_content_preserves_image_and_pdf(self):
        llm = _make_llm()
        _, items = llm._convert_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,abc"},
                        },
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": "pdf-data",
                            },
                        },
                    ],
                }
            ]
        )

        assert items == [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "inspect"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,abc",
                    },
                    {
                        "type": "input_file",
                        "file_data": "data:application/pdf;base64,pdf-data",
                        "filename": "document.pdf",
                    },
                ],
            }
        ]


class TestReasoningEffort:
    def test_effort_becomes_reasoning_block(self):
        llm = _make_llm(reasoning_effort="medium")  # gpt-5.4 → supports_thinking
        kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert kwargs["reasoning"] == {"effort": "medium"}

    def test_no_effort_no_reasoning(self):
        llm = _make_llm()
        kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert "reasoning" not in kwargs

    def test_incapable_model_ignores_effort(self):
        llm = _make_llm(reasoning_effort="high")
        llm.model = "gpt-4.1"  # vision+web but no thinking
        kwargs = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert "reasoning" not in kwargs


def test_native_schema_uses_responses_text_format() -> None:
    llm = _make_llm()

    request = llm.native_schema_request(
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        }
    )

    assert request is not None
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["name"] == "mote_output"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["additionalProperties"] is False


class TestToolSearchPair:
    """A SearchTools discovery result → tool_search_call + tool_search_output pair."""

    def _pair(self, llm, refs):
        # _defer_specs is normally populated by _cons_kwargs from the tools list;
        # seed it directly for the isolated _convert_messages test.
        llm._defer_specs = {
            "ConvertImage": {
                "type": "function",
                "name": "ConvertImage",
                "description": "Convert an image.",
                "parameters": {"type": "object", "properties": {}},
            }
        }
        _, items = llm._convert_messages(
            [
                {
                    "role": "tool",
                    "tool_call_id": "call_search",
                    "content": "Revealed: ConvertImage",
                    "_tool_references": refs,
                }
            ],
            render_tool_references=True,
        )
        return items

    def test_discovery_becomes_call_output_pair(self):
        llm = _make_llm()
        items = self._pair(llm, ["ConvertImage"])
        assert len(items) == 2
        call, output = items
        assert call["type"] == "tool_search_call"
        assert call["execution"] == "client"
        assert call["call_id"] == "call_search"
        assert output["type"] == "tool_search_output"
        assert output["execution"] == "client"
        assert output["call_id"] == "call_search"

    def test_embedded_tool_carries_defer_loading(self):
        llm = _make_llm()
        items = self._pair(llm, ["ConvertImage"])
        tools = items[1]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "ConvertImage"
        assert tools[0]["defer_loading"] is True
        # FLAT Responses function shape.
        assert tools[0]["type"] == "function"
        assert "function" not in tools[0]

    def test_unknown_ref_skipped(self):
        llm = _make_llm()
        items = self._pair(llm, ["ConvertImage", "NoSuchTool"])
        names = {t["name"] for t in items[1]["tools"]}
        assert names == {"ConvertImage"}

    def test_private_key_never_reaches_wire(self):
        llm = _make_llm()
        items = self._pair(llm, ["ConvertImage"])
        for item in items:
            assert "_tool_references" not in item


class TestToolSearchCorpusGate:
    """A tool_search pair is only valid alongside the deferred corpus it expands.

    Without a deferred corpus in the request (any toolless ``aask`` — summarize,
    dedup guards, routing) a history-carried ``_tool_references`` degrades to an
    ordinary ``function_call_output``, so the API never sees an orphaned search
    pair it would reject.
    """

    def _reveal_msg(self):
        return {
            "role": "tool",
            "tool_call_id": "call_search",
            "content": "Revealed: ConvertImage",
            "_tool_references": ["ConvertImage"],
        }

    def test_default_degrades_to_function_call_output(self):
        llm = _make_llm()
        _, items = llm._convert_messages([self._reveal_msg()])
        assert len(items) == 1
        assert items[0]["type"] == "function_call_output"
        assert items[0]["call_id"] == "call_search"
        assert items[0]["output"] == "Revealed: ConvertImage"

    def test_toolless_cons_kwargs_degrades(self):
        # No tools (the aask path) → discovery result becomes plain output, no pair.
        llm = _make_llm()
        kw = llm._cons_kwargs([self._reveal_msg()])
        types = [it.get("type") for it in kw["input"]]
        assert "tool_search_call" not in types
        assert "function_call_output" in types

    def test_cons_kwargs_with_deferred_corpus_renders_pair(self):
        # A deferred corpus member present (the aask_tool path) → the pair IS emitted.
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [self._reveal_msg()],
            tools=[
                {
                    "type": "function",
                    "name": "ConvertImage",
                    "description": "Convert an image.",
                    "parameters": {"type": "object", "properties": {}},
                    "defer_loading": True,
                }
            ],
        )
        types = [it.get("type") for it in kw["input"]]
        assert "tool_search_call" in types
        assert "tool_search_output" in types


class TestConsKwargs:
    def _tools(self):
        # Native-defer corpus member (flat Responses shape, defer_loading stamped)
        # + an ordinary tool.
        return [
            {
                "type": "function",
                "name": "ConvertImage",
                "description": "Convert an image.",
                "parameters": {"type": "object", "properties": {}},
                "defer_loading": True,
            },
            {
                "type": "function",
                "name": "Read",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {}},
            },
        ]

    def test_injects_client_tool_search_when_deferred(self):
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [{"role": "system", "content": "g"}, {"role": "user", "content": "hi"}],
            tools=self._tools(),
        )
        types = [t.get("type") for t in kw["tools"]]
        assert "tool_search" in types
        search = next(t for t in kw["tools"] if t["type"] == "tool_search")
        assert search["execution"] == "client"

    def test_defer_specs_indexed_for_embedding(self):
        llm = _make_llm()
        llm._cons_kwargs(
            [{"role": "user", "content": "hi"}],
            tools=self._tools(),
        )
        # Corpus member indexed WITHOUT the defer_loading key (added back on embed).
        assert "ConvertImage" in llm._defer_specs
        assert "defer_loading" not in llm._defer_specs["ConvertImage"]
        assert "Read" not in llm._defer_specs

    def test_no_tool_search_when_nothing_deferred(self):
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "name": "Read",
                    "description": "Read a file.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        )
        assert all(t.get("type") != "tool_search" for t in kw["tools"])

    def test_system_becomes_instructions_kwarg(self):
        llm = _make_llm()
        kw = llm._cons_kwargs([{"role": "system", "content": "guide"}, {"role": "user", "content": "hi"}])
        assert kw["instructions"] == "guide"

    def test_strips_private_wire_keys(self):
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [{"role": "user", "content": "hi"}],
            _cache_intent="something",
            _tool_references=["X"],
        )
        assert "_cache_intent" not in kw
        assert "_tool_references" not in kw

    def test_empty_input_gets_dummy_message(self):
        # A history that reduces to zero message items must not send input=[].
        llm = _make_llm()
        kw = llm._cons_kwargs([{"role": "system", "content": "only system"}])
        assert kw["input"], "empty input must be backfilled with a dummy user message"
        assert kw["input"][0]["role"] == "user"

    def test_tool_choice_flat_shape(self):
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [{"role": "user", "content": "hi"}],
            tool_choice={"type": "function", "function": {"name": "Read"}},
        )
        # Responses uses the FLAT forced-function shape.
        assert kw["tool_choice"] == {"type": "function", "name": "Read"}


class TestResponseParse:
    def _rsp(self, output, output_text=None):
        return SimpleNamespace(output=output, output_text=output_text, usage=None)

    def test_output_text_aggregate(self):
        llm = _make_llm()
        assert llm.get_choice_text(self._rsp([], output_text="hello")) == "hello"

    def test_output_text_fallback_walk(self):
        llm = _make_llm()
        msg = SimpleNamespace(
            type="message",
            content=[SimpleNamespace(type="output_text", text="walked")],
        )
        assert llm.get_choice_text(self._rsp([msg])) == "walked"

    def test_function_call_becomes_tool_call(self):
        llm = _make_llm()
        fc = SimpleNamespace(
            type="function_call",
            call_id="call_9",
            name="Read",
            arguments='{"path": "/x"}',
            id="fc_9",
        )
        calls = llm.get_choice_tool_calls(self._rsp([fc]))
        assert calls == [{"id": "call_9", "name": "Read", "arguments": {"path": "/x"}}]

    def test_malformed_arguments_repaired(self):
        llm = _make_llm()
        fc = SimpleNamespace(
            type="function_call",
            call_id="c",
            name="Read",
            arguments='{"path": "/x"',  # missing closing brace
            id="fc",
        )
        calls = llm.get_choice_tool_calls(self._rsp([fc]))
        assert calls[0]["arguments"] == {"path": "/x"}

    def test_tool_search_call_becomes_search_tools(self):
        llm = _make_llm()
        tsc = SimpleNamespace(
            type="tool_search_call",
            call_id="ts_1",
            arguments={"queries": ["image", "convert"]},
            id="ts_1",
        )
        calls = llm.get_choice_tool_calls(self._rsp([tsc]))
        assert calls == [{"id": "ts_1", "name": "SearchTools", "arguments": {"query": "image convert"}}]

    def test_tool_search_call_string_arguments(self):
        llm = _make_llm()
        tsc = SimpleNamespace(
            type="tool_search_call",
            call_id="ts_2",
            arguments='{"queries": ["x"]}',
            id="ts_2",
        )
        calls = llm.get_choice_tool_calls(self._rsp([tsc]))
        assert calls[0]["name"] == "SearchTools"
        assert calls[0]["arguments"] == {"query": "x"}


class TestAwebSearchCapabilityGate:
    """``aweb_search`` refuses (NotImplementedError) before any network call when
    the routed model can't drive server-side search, so the WebSearch tool degrades
    cleanly rather than firing a doomed request whose error it cannot catch.
    """

    def test_incapable_model_raises_notimplemented(self):
        llm = _make_llm()
        llm.model = "gpt-3.5-turbo"  # not in WEB_SEARCH_MODELS (pre-4o, no server search)
        with pytest.raises(NotImplementedError):
            asyncio.run(llm.aweb_search("anything"))


class TestExtractWebSearchHits:
    """``_extract_web_search_hits`` parses the Responses ``web_search`` reply.

    OpenAI's built-in ``web_search`` surfaces its sources as ``url_citation``
    annotations on the assistant message's ``output_text`` blocks (NOT a distinct
    result block). We walk output → message → output_text → annotations, dedupe by
    URL preserving first-seen order, and tolerate both dict and attribute-style
    annotations.
    """

    @staticmethod
    def _msg_with_annotations(annotations):
        return SimpleNamespace(
            type="message",
            content=[SimpleNamespace(type="output_text", text="answer", annotations=annotations)],
        )

    def test_parses_url_citations(self):
        rsp = SimpleNamespace(
            output=[
                self._msg_with_annotations(
                    [
                        {"type": "url_citation", "url": "https://foo.com", "title": "Foo"},
                        {"type": "url_citation", "url": "https://bar.com", "title": "Bar"},
                    ]
                )
            ]
        )
        hits = OpenAIResponsesLLM._extract_web_search_hits(rsp)
        assert [(h.title, h.url) for h in hits] == [
            ("Foo", "https://foo.com"),
            ("Bar", "https://bar.com"),
        ]

    def test_dedupes_by_url_preserving_order(self):
        rsp = SimpleNamespace(
            output=[
                self._msg_with_annotations(
                    [
                        {"type": "url_citation", "url": "https://foo.com", "title": "Foo"},
                        {"type": "url_citation", "url": "https://foo.com", "title": "Foo again"},
                        {"type": "url_citation", "url": "https://baz.com", "title": "Baz"},
                    ]
                )
            ]
        )
        hits = OpenAIResponsesLLM._extract_web_search_hits(rsp)
        assert [h.url for h in hits] == ["https://foo.com", "https://baz.com"]

    def test_non_url_citation_annotations_skipped(self):
        rsp = SimpleNamespace(
            output=[
                self._msg_with_annotations(
                    [
                        {"type": "file_citation", "url": "https://ignored.com"},
                        SimpleNamespace(type="url_citation", url="https://ok.com", title="Ok"),
                    ]
                )
            ]
        )
        hits = OpenAIResponsesLLM._extract_web_search_hits(rsp)
        assert [(h.title, h.url) for h in hits] == [("Ok", "https://ok.com")]

    def test_empty_output_yields_no_hits(self):
        assert OpenAIResponsesLLM._extract_web_search_hits(SimpleNamespace(output=None)) == []
