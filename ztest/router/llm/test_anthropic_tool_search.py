#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Anthropic server-side tool-search (defer_loading) wire path.

The native Anthropic channel implements Tool Search via the *custom* path: mote's
own ``SearchTools`` returns a tool_result whose content is a list of
``tool_reference`` blocks, which the API expands into full definitions. Corpus
tools carry ``defer_loading:true`` on the wire (keyed on membership, not the
revealed set) so the ``tools=`` prefix is byte-stable and the prompt cache is
preserved across reveals. The tools cache breakpoint must skip deferred tools
(``defer_loading`` + ``cache_control`` on the same tool = API 400).

No network: only ``_convert_messages`` / ``_cons_kwargs`` (pure wire builders).
"""
from __future__ import annotations

import asyncio

import pytest

from mote.contracts.config.llm import LLMConfig
from mote.product.integrations.models.anthropic import AnthropicLLM


def _make_llm(**overrides):
    cfg = LLMConfig(
        api_type="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-opus-4-8",
        api_key="sk-test",  # pragma: allowlist secret
        max_token=2048,
        **overrides,
    )
    return AnthropicLLM(cfg)


class TestToolReferenceBlocks:
    def test_tool_result_rendered_as_tool_reference_blocks(self):
        llm = _make_llm()
        _, conv = llm._convert_messages(
            [
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "Revealed 1 tool(s): ConvertImage",
                    "_tool_references": ["ConvertImage"],
                }
            ],
            render_tool_references=True,
        )
        # The tool result becomes a tool_result block inside a user turn.
        assert len(conv) == 1
        block = conv[0]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "call_1"
        # Content is the reference block list, NOT the stringified text.
        assert block["content"] == [{"type": "tool_reference", "tool_name": "ConvertImage"}]

    def test_multiple_references(self):
        llm = _make_llm()
        _, conv = llm._convert_messages(
            [
                {
                    "role": "tool",
                    "tool_call_id": "c",
                    "content": "text",
                    "_tool_references": ["A", "B"],
                }
            ],
            render_tool_references=True,
        )
        assert conv[0]["content"][0]["content"] == [
            {"type": "tool_reference", "tool_name": "A"},
            {"type": "tool_reference", "tool_name": "B"},
        ]

    def test_private_key_never_reaches_wire(self):
        llm = _make_llm()
        _, conv = llm._convert_messages(
            [{"role": "tool", "tool_call_id": "c", "content": "t", "_tool_references": ["A"]}],
            render_tool_references=True,
        )
        # The tool_result block carries no private routing key.
        assert "_tool_references" not in conv[0]["content"][0]

    def test_ordinary_tool_result_unchanged(self):
        # No _tool_references → content stays the stringified text (existing path).
        llm = _make_llm()
        _, conv = llm._convert_messages([{"role": "tool", "tool_call_id": "c", "content": "plain output"}])
        assert conv[0]["content"][0]["content"] == "plain output"

    def test_empty_references_falls_back_to_text(self):
        llm = _make_llm()
        _, conv = llm._convert_messages(
            [{"role": "tool", "tool_call_id": "c", "content": "plain", "_tool_references": []}],
            render_tool_references=True,
        )
        assert conv[0]["content"][0]["content"] == "plain"


class TestToolReferenceCorpusGate:
    """A tool_reference is only valid alongside the corpus it expands against.

    Without a deferred-tool corpus in the request (any toolless ``aask`` —
    summarize, dedup guards, routing) a history-carried ``_tool_references``
    degrades to its plain stringified text, so the API never sees an orphaned
    reference block it would reject with a 400.
    """

    def _reveal_msg(self):
        return {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Revealed 1 tool(s): ConvertImage",
            "_tool_references": ["ConvertImage"],
        }

    def test_default_degrades_to_text(self):
        # Default (no render flag) → the reference degrades to stringified text.
        llm = _make_llm()
        _, conv = llm._convert_messages([self._reveal_msg()])
        assert conv[0]["content"][0]["content"] == "Revealed 1 tool(s): ConvertImage"

    def test_toolless_cons_kwargs_degrades(self):
        # A completion with NO tools (the aask path) must not emit a reference.
        llm = _make_llm()
        kw = llm._cons_kwargs([self._reveal_msg()])
        block = kw["messages"][0]["content"][0]
        assert block["content"] == "Revealed 1 tool(s): ConvertImage"
        assert "tools" not in kw

    def test_cons_kwargs_with_corpus_renders_reference(self):
        # tools present (the aask_tool path) → the reference block IS emitted.
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [self._reveal_msg()],
            tools=[{"name": "ConvertImage", "input_schema": {"type": "object"}, "defer_loading": True}],
        )
        block = kw["messages"][0]["content"][0]
        assert block["content"] == [{"type": "tool_reference", "tool_name": "ConvertImage"}]


def _cache_control_tool_names(kw):
    """Names of tools in ``kw`` carrying a cache_control marker."""
    return [t.get("name") for t in kw["tools"] if isinstance(t, dict) and "cache_control" in t]


class TestCacheBreakpointSkipsDeferred:
    def test_marker_on_last_non_deferred_tool(self):
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [{"role": "system", "content": "guide"}, {"role": "user", "content": "hi"}],
            tools=[
                {"name": "Read", "input_schema": {"type": "object"}},
                {"name": "SearchTools", "input_schema": {"type": "object"}},
                {"name": "ConvertImage", "input_schema": {"type": "object"}, "defer_loading": True},
            ],
        )
        marked = _cache_control_tool_names(kw)
        # Exactly the last NON-deferred tool (SearchTools), never the deferred one.
        assert marked == ["SearchTools"]
        conv = next(t for t in kw["tools"] if t["name"] == "ConvertImage")
        assert "cache_control" not in conv
        assert conv["defer_loading"] is True

    def test_no_deferred_marks_last_tool_as_before(self):
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [{"role": "system", "content": "guide"}, {"role": "user", "content": "hi"}],
            tools=[
                {"name": "Read", "input_schema": {"type": "object"}},
                {"name": "Write", "input_schema": {"type": "object"}},
            ],
        )
        assert _cache_control_tool_names(kw) == ["Write"]

    def test_all_deferred_skips_tools_breakpoint(self):
        llm = _make_llm()
        kw = llm._cons_kwargs(
            [{"role": "system", "content": "guide"}, {"role": "user", "content": "hi"}],
            tools=[
                {"name": "A", "input_schema": {"type": "object"}, "defer_loading": True},
                {"name": "B", "input_schema": {"type": "object"}, "defer_loading": True},
            ],
        )
        # No tool gets a marker (would be a 400); system + messages still do.
        assert _cache_control_tool_names(kw) == []
        assert any("cache_control" in b for b in kw["system"])


class TestAwebSearchCapabilityGate:
    """``aweb_search`` refuses (NotImplementedError) before touching the network
    when the routed model can't drive server-side search — so the WebSearch tool's
    ``except NotImplementedError`` degradation actually fires instead of an opaque
    API error slipping through on a doomed request.
    """

    def test_incapable_model_raises_notimplemented(self):
        llm = _make_llm()
        llm.model = "claude-3-opus"  # not in WEB_SEARCH_MODELS
        with pytest.raises(NotImplementedError):
            asyncio.run(llm.aweb_search("anything"))


class TestExtractWebSearchHits:
    """``_extract_web_search_hits`` parses the server-side ``web_search`` reply.

    The secondary ``aweb_search`` call returns an Anthropic message whose
    ``content`` carries ``web_search_tool_result`` blocks; each block's ``content``
    is a list of result items with ``title`` / ``url``. We pull those into
    ``WebSearchHit``s, tolerating both attribute-style objects and plain dicts,
    and skipping error blocks (no list content).
    """

    def test_parses_result_blocks(self):
        from types import SimpleNamespace

        rsp = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="here you go"),
                SimpleNamespace(
                    type="web_search_tool_result",
                    content=[
                        SimpleNamespace(title="Foo", url="https://foo.com"),
                        SimpleNamespace(title="Bar", url="https://bar.com"),
                    ],
                ),
            ]
        )
        hits = AnthropicLLM._extract_web_search_hits(rsp)
        assert [(h.title, h.url) for h in hits] == [
            ("Foo", "https://foo.com"),
            ("Bar", "https://bar.com"),
        ]

    def test_dict_items_supported(self):
        from types import SimpleNamespace

        rsp = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="web_search_tool_result",
                    content=[{"title": "Baz", "url": "https://baz.com"}],
                )
            ]
        )
        hits = AnthropicLLM._extract_web_search_hits(rsp)
        assert [(h.title, h.url) for h in hits] == [("Baz", "https://baz.com")]

    def test_error_block_and_no_url_skipped(self):
        from types import SimpleNamespace

        rsp = SimpleNamespace(
            content=[
                # An error block: type matches but content is not a list.
                SimpleNamespace(type="web_search_tool_result", content=None),
                SimpleNamespace(
                    type="web_search_tool_result",
                    content=[
                        {"title": "No URL"},  # no url -> skipped
                        {"title": "Ok", "url": "https://ok.com"},
                    ],
                ),
            ]
        )
        hits = AnthropicLLM._extract_web_search_hits(rsp)
        assert [(h.title, h.url) for h in hits] == [("Ok", "https://ok.com")]

    def test_empty_response_yields_no_hits(self):
        from types import SimpleNamespace

        assert AnthropicLLM._extract_web_search_hits(SimpleNamespace(content=None)) == []
