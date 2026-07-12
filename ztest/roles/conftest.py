#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures + duck-typed fakes for the roles test suite.

Key facts the fixtures encode:

- A :class:`Role` is constructed with ``context=`` — the router's LLM-build
  ``Context``. The real ``Context`` builds entirely offline (it only constructs
  provider objects lazily and never touches the network at build time), so the
  ``context`` fixture hands out a real one. Tests that would otherwise issue an
  LLM call inject the lightweight fakes below instead.
- ``Role.router`` / ``Role.context_manager`` are lazy and cached. Tests that
  want to bypass them pre-seed the private ``_router`` / ``_context_manager`` /
  ``_think_engine`` slots with the fakes here.
"""
from __future__ import annotations

import pytest

from mote.roles import Role


class FakeLLM:
    """Minimal duck-typed stand-in for a BaseLLM."""

    def __init__(self, name: str = "fake", reply: str = "summary-text"):
        self.name = name
        self.reply = reply
        self.model = "gpt-4"
        self.aask_calls: list = []
        self._fallback_supplier = None

    async def aask(self, msg, system_msgs=None, stream=True, **kwargs):
        self.aask_calls.append(msg)
        return self.reply


class FakeRouter:
    """Stand-in for LLMRouter exposing only what the Role touches."""

    def __init__(self, llm: FakeLLM | None = None):
        self.llm = llm or FakeLLM()
        self.task_calls: list[str] = []

    def route_for_task(self, task: str) -> FakeLLM:
        self.task_calls.append(task)
        return self.llm

    def route(self, *, name=None, llm_config=None) -> FakeLLM:
        return self.llm


class _FakeThinkResult:
    def __init__(self, content: str = "", is_empty: bool = True):
        self.content = content
        self.is_empty = is_empty


class FakeThinkEngine:
    def __init__(self, result: _FakeThinkResult | None = None):
        self.result = result or _FakeThinkResult()


class FakeContextManager:
    """Duck-typed ContextManager exposing the get() the Role relies on."""

    def __init__(self, messages=None):
        self._messages = list(messages or [])
        self.get_calls: list[int] = []

    def get(self, k=0):
        self.get_calls.append(k)
        if k <= 0:
            return list(self._messages)
        return self._messages[-k:]


class FakeEnv:
    """Duck-typed Environment for routing / human-channel tests."""

    def __init__(self, desc: str = "", roles: dict | None = None):
        self.desc = desc
        self.roles = roles or {}
        self.published: list = []
        self.set_addresses_calls: list = []
        self.human_questions: list = []
        self.human_replies: list = []
        self.human_response = "ok"

    def set_addresses(self, role, addresses):
        self.set_addresses_calls.append((role, set(addresses)))

    def role_names(self):
        return [r.name for r in self.roles.values()]

    def publish_message(self, msg):
        self.published.append(msg)

    async def ask_user(self, question, sent_from=None):
        self.human_questions.append((question, sent_from))
        return self.human_response

    async def reply_to_user(self, content, sent_from=None):
        self.human_replies.append((content, sent_from))
        return "delivered"


@pytest.fixture
def context():
    """A real router Context (builds offline, no network)."""
    from mote.router.llm.context import Context

    return Context()


@pytest.fixture
def role(context):
    """A bound Role with a real Context but no env."""
    return Role(name="Alice", context=context)
