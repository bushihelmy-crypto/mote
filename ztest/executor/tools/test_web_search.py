#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the ``WebSearch`` tool (mote.executor.tools.web_search).

``WebSearch`` issues an isolated secondary LLM call (via the Role's ``web_search``
capability) carrying the provider's server-side web-search tool and renders the
returned hits as a CC-aligned markdown link list. These tests drive it through
the same ``CapRole`` capability allowlist the real Role publishes, with the
``web_search`` capability scripted (hit list or NotImplementedError) — fully
offline, no network, no real LLM.
"""
from __future__ import annotations

import pytest

from mote.common.exception import ToolNotConfiguredError
from mote.executor.tools.web_search import WebSearch
from mote.router.llm.llm_response import WebSearchHit

from .conftest import CapRole, bind, run


def _call(role: CapRole, **kwargs):
    tool = bind(WebSearch(), role)
    return run(tool.call(**kwargs))


HITS = [
    WebSearchHit(title="Python docs", url="https://docs.python.org", snippet="official docs"),
    WebSearchHit(title="Real Python", url="https://realpython.com"),
]


class TestFormat:
    def test_hits_rendered_as_links(self):
        role = CapRole()
        role.web_search_hits = HITS
        result = _call(role, query="python tutorial")
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
        role = CapRole()
        role.web_search_hits = HITS
        _call(role, query="cats", allowed_domains=["a.com"])
        assert len(role.web_search_calls) == 1
        query, kwargs = role.web_search_calls[0]
        assert query == "cats"
        assert kwargs["allowed_domains"] == ["a.com"]
        # num_results is a client-side result cap, NOT the API's search budget
        # (max_uses) — the tool no longer conflates the two, so it is not forwarded.
        assert "max_uses" not in kwargs

    def test_num_results_caps_returned_links(self):
        # num_results truncates the hit list client-side; it does not change how
        # many searches the API runs (that stays the provider default).
        role = CapRole()
        role.web_search_hits = HITS  # two hits
        result = _call(role, query="python", num_results=1)
        assert "Python docs" in result.output
        assert "Real Python" not in result.output

    def test_no_results_message(self):
        role = CapRole()
        role.web_search_hits = []
        result = _call(role, query="obscure query")
        assert result.success
        assert "No search results found." in result.output
        # Reminder still present even with zero hits (matches CC's shape).
        assert "REMINDER" in result.output


class TestDegradation:
    def test_unavailable_raises_not_configured(self):
        # web_search_hits None → capability raises NotImplementedError → the tool
        # raises ToolNotConfiguredError naming the config path + steering to
        # WebBrowser (option 1A: no scraper fallback). The executor turns this
        # into ToolResult(success=False) with the message as output.
        role = CapRole()  # web_search_hits defaults to None
        with pytest.raises(ToolNotConfiguredError) as excinfo:
            _call(role, query="anything")
        msg = str(excinfo.value)
        assert "unavailable" in msg.lower()
        assert "models.tasks.web_search" in msg
        assert "WebBrowser" in msg


class TestValidation:
    def test_empty_query_rejected(self):
        from mote.common.exception import ToolValidationError

        role = CapRole()
        role.web_search_hits = HITS
        with pytest.raises(ToolValidationError, match="Missing query"):
            _call(role, query="   ")
        # The capability is never reached on a validation failure.
        assert role.web_search_calls == []

    def test_allowed_and_blocked_mutually_exclusive(self):
        from mote.common.exception import ToolValidationError

        role = CapRole()
        role.web_search_hits = HITS
        with pytest.raises(ToolValidationError, match="Cannot specify both"):
            _call(role, query="cats", allowed_domains=["a.com"], blocked_domains=["b.com"])
        assert role.web_search_calls == []


class TestSchema:
    def test_description_carries_operating_manual(self):
        # Docstring-native: the full operating manual (the Sources: requirement +
        # current-year guidance) lives in the call() docstring body, so it rides
        # the auto-generated wire description.
        schema = WebSearch.get_schema()
        assert "Sources:" in schema["description"]
        assert "current year" in schema["description"]

    def test_native_schema_carries_all_params(self):
        native = WebSearch.get_native_schema()
        props = native["input_schema"]["properties"]
        for name in ("query", "allowed_domains", "blocked_domains", "num_results"):
            assert name in props
        # No-op Exa-only params (livecrawl/search_type/context_max_characters)
        # were dropped — they had no server-side analog and only misled the model.
        for name in ("livecrawl", "search_type", "context_max_characters"):
            assert name not in props
