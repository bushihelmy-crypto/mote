#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for tool-call argument JSON repair in ``OpenAILLM.get_choice_tool_calls``.

A model emitting a large multi-line string argument (e.g. Write's whole file
body) sometimes produces invalid JSON — unescaped newlines/quotes or a
truncated tail. Rather than dropping the whole argument (the old ``{}``
fallback), the parser routes the ``JSONDecodeError`` path through
``json_repair`` to recover the call.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

from mote.product.integrations.models.openai_chat import OpenAILLM


class _FakeLLM:
    """Minimal stand-in carrying only the two methods under test (no config)."""

    _repair_tool_arguments = staticmethod(OpenAILLM._repair_tool_arguments)
    get_choice_tool_calls = OpenAILLM.get_choice_tool_calls


def _rsp(name: str, arguments) -> NS:
    """Build a minimal ChatCompletion-shaped object with one tool call."""
    call = NS(id="call_1", function=NS(name=name, arguments=arguments))
    return NS(choices=[NS(message=NS(tool_calls=[call]))])


class TestRepairToolArguments:
    def test_unescaped_newline_recovered(self):
        # A raw newline inside the string value is invalid JSON.
        assert OpenAILLM._repair_tool_arguments('{"input": "a\nb"}') == {"input": "a\nb"}

    def test_truncated_tail_recovered(self):
        assert OpenAILLM._repair_tool_arguments('{"input": "abc') == {"input": "abc"}

    def test_unsalvageable_returns_empty(self):
        assert OpenAILLM._repair_tool_arguments("totally not json") == {}

    def test_empty_returns_empty(self):
        assert OpenAILLM._repair_tool_arguments("") == {}

    def test_none_returns_empty(self):
        assert OpenAILLM._repair_tool_arguments(None) == {}


class TestGetChoiceToolCallsIntegration:
    def test_valid_json_unchanged(self):
        calls = _FakeLLM().get_choice_tool_calls(_rsp("Write", '{"content": "ok"}'))
        assert calls == [{"id": "call_1", "name": "Write", "arguments": {"content": "ok"}}]

    def test_malformed_json_repaired_not_dropped(self):
        # The old behaviour returned arguments={}; now the body is recovered.
        body = "def f():\n    return 1\n\nclass A:\n    pass"
        bad = '{"content": "' + body + '"}'  # raw newlines -> invalid JSON
        calls = _FakeLLM().get_choice_tool_calls(_rsp("Write", bad))
        assert len(calls) == 1
        assert calls[0]["arguments"]["content"] == body

    def test_unsalvageable_falls_back_to_empty(self):
        calls = _FakeLLM().get_choice_tool_calls(_rsp("Write", "::garbage::"))
        assert calls[0]["arguments"] == {}

    def test_text_only_response_yields_no_calls(self):
        rsp = NS(choices=[NS(message=NS(tool_calls=None))])
        assert _FakeLLM().get_choice_tool_calls(rsp) == []

    def test_no_choices_yields_no_calls(self):
        assert _FakeLLM().get_choice_tool_calls(NS(choices=[])) == []
