#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures + duck-typed fakes for the prompts test suite.

The prompts package is almost entirely pure functions over string templates,
so the only collaborators that need faking are the four ``ThinkSubsystems``
members PromptBuilder queries: ``config`` (only ``config.context`` is read),
``llm`` (only ``llm.model``), ``executor`` (two ``get_*_tool_schemas`` calls)
and ``skill_manager`` (its optional ``injector``). Everything else is data the
Role would push through ``ThinkInputs``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class FakeLLM:
    """Minimal stand-in — PromptBuilder only reads ``.model``."""

    def __init__(self, model: str = "gpt-4"):
        self.model = model


class FakeExecutor:
    """Exposes the two schema getters collect_context() serializes to JSON."""

    def __init__(self, tools=None, mcp_tools=None, pipeline_tools=None):
        self._tools = tools if tools is not None else [{"name": "Read"}]
        self._mcp = mcp_tools if mcp_tools is not None else []
        self._pipeline = pipeline_tools if pipeline_tools is not None else []

    def get_tool_schemas(self):
        return self._tools

    def get_mcp_tool_schemas(self):
        return self._mcp

    def get_pipeline_tool_schemas(self):
        return self._pipeline


class FakeInjector:
    """Skill injector — build_content/build_index return canned text; build_guide
    returns the static loading guide (what the system prompt now uses)."""

    def __init__(self, content: str = "SKILLS_TEXT", guide: str = "SKILL_GUIDE"):
        self.content = content
        self.guide = guide
        self.max_tokens_seen = None

    def build_content(self, max_tokens=None):
        self.max_tokens_seen = max_tokens
        return self.content

    def build_index(self, max_tokens=None, only_names=None):
        self.max_tokens_seen = max_tokens
        return self.content

    def build_guide(self):
        return self.guide


class FakeSkillManager:
    def __init__(self, injector=None):
        self.injector = injector


def make_config(
    *,
    compaction_enabled=False,
    protected_recent_messages=8,
    bggraph_enabled=False,
    skills_enabled=True,
    response_language="chinese",
):
    """Minimal config stand-in.

    PromptBuilder reads ``context.compaction`` / ``context.bggraph`` /
    ``context.skills`` and ``models.response_language``.
    """
    compaction = SimpleNamespace(
        enabled=compaction_enabled,
        protected_recent_messages=protected_recent_messages,
    )
    bggraph = SimpleNamespace(enabled=bggraph_enabled)
    skills = SimpleNamespace(enabled=skills_enabled)
    context = SimpleNamespace(compaction=compaction, bggraph=bggraph, skills=skills)
    models = SimpleNamespace(response_language=response_language)
    return SimpleNamespace(context=context, models=models)


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def executor():
    return FakeExecutor()


@pytest.fixture
def skill_manager():
    return FakeSkillManager()


@pytest.fixture
def config():
    return make_config()
