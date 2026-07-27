#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the ``WebSearch`` tool (mote.product.toolsets.builtin.web_search).

``WebSearch`` submits one PURE logical service call and renders the returned hits
as a CC-aligned markdown link list. These tests inject the narrow
``invoke_service`` capability directly — fully offline, no network, no real LLM.
"""
from __future__ import annotations

import pytest

from mote.contracts.errors.services import ServiceCallExhaustedError
from mote.contracts.models import WebSearchHit
from mote.contracts.services import ServiceExecutionSemantics
from mote.product.toolsets.builtin.web_search import WebSearch
from mote.runtime.errors import ToolNotConfiguredError
from mote.runtime.tools.definitions import native_definition

from .conftest import run


def _tool(hits=None, *, error: Exception | None = None) -> WebSearch:
    config = type("SearchConfig", (), {"backend": "provider"})()
    tool = WebSearch(config)
    tool.service_calls = []

    async def invoke_service(**kwargs):
        tool.service_calls.append(kwargs)
        if error is not None:
            raise error
        selected = HITS if hits is None else hits
        return {"hits": [{"title": hit.title, "url": hit.url, "snippet": hit.snippet} for hit in selected]}

    tool.invoke_service = invoke_service
    return tool


def _call(tool: WebSearch, **kwargs):
    return run(tool.call(**kwargs))


HITS = [
    WebSearchHit(title="Python docs", url="https://docs.python.org", snippet="official docs"),
    WebSearchHit(title="Real Python", url="https://realpython.com"),
]


class TestFormat:
    def test_hits_rendered_as_links(self):
        result = _call(_tool(), query="python tutorial")
        assert result.success
        assert 'Web search results for query: "python tutorial"' in result.output
        assert "Links:" in result.output
        # snippet appended after ": " when present, omitted otherwise.
        assert "- [Python docs](https://docs.python.org): official docs" in result.output
        assert "- [Real Python](https://realpython.com)" in result.output
        # The mandatory sources reminder always trails.
        assert result.output.rstrip().endswith(
            "REMINDER: You MUST include the sources above in your response to the user using markdown hyperlinks."
        )

    def test_query_forwarded_to_capability(self):
        tool = _tool()
        _call(tool, query="cats", allowed_domains=["a.com"])
        assert len(tool.service_calls) == 1
        call = tool.service_calls[0]
        assert call["route_id"] == "web.search"
        assert call["capability"] == "web.search"
        assert call["operation_key"] == "query"
        assert call["semantics"] is ServiceExecutionSemantics.PURE
        assert call["payload"]["query"] == "cats"
        assert call["payload"]["allowed_domains"] == ["a.com"]
        assert call["payload"]["max_uses"] == 8

    def test_num_results_caps_returned_links(self):
        # num_results truncates the hit list client-side; it does not change how
        # many searches the API runs (that stays the provider default).
        result = _call(_tool(), query="python", num_results=1)
        assert "Python docs" in result.output
        assert "Real Python" not in result.output

    def test_no_results_message(self):
        result = _call(_tool([]), query="obscure query")
        assert result.success
        assert "No search results found." in result.output
        # Reminder still present even with zero hits (matches CC's shape).
        assert "REMINDER" in result.output


class TestDegradation:
    def test_unavailable_raises_not_configured(self):
        error = ServiceCallExhaustedError("search unavailable")
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            _call(_tool(error=error), query="anything")
        msg = str(excinfo.value)
        assert "unavailable" in msg.lower()
        assert "models.tasks.web_search" in msg
        assert "WebBrowser" in msg


class TestValidation:
    def test_empty_query_rejected(self):
        from mote.runtime.errors import ToolValidationError

        tool = _tool()
        with pytest.raises(ToolValidationError, match="Missing query"):
            _call(tool, query="   ")
        # The capability is never reached on a validation failure.
        assert tool.service_calls == []

    def test_allowed_and_blocked_mutually_exclusive(self):
        from mote.runtime.errors import ToolValidationError

        tool = _tool()
        with pytest.raises(ToolValidationError, match="Cannot specify both"):
            _call(tool, query="cats", allowed_domains=["a.com"], blocked_domains=["b.com"])
        assert tool.service_calls == []

    def test_started_call_reenters_service_gateway(self):
        assert _tool().can_resume_started_call("call-id") is True


class TestSchema:
    def test_description_carries_operating_manual(self):
        # Docstring-native: the full operating manual (the Sources: requirement +
        # current-year guidance) lives in the call() docstring body, so it rides
        # the auto-generated wire description.
        schema = native_definition(WebSearch).render(_tool())
        assert "Sources:" in schema["description"]
        assert "current year" in schema["description"]

    def test_native_schema_carries_all_params(self):
        native = native_definition(WebSearch).render(_tool())
        props = native["input_schema"]["properties"]
        for name in ("query", "allowed_domains", "blocked_domains", "num_results"):
            assert name in props
        # No-op Exa-only params (livecrawl/search_type/context_max_characters)
        # were dropped — they had no server-side analog and only misled the model.
        for name in ("livecrawl", "search_type", "context_max_characters"):
            assert name not in props
