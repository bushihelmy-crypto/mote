#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures for the think-engine test suite.

The fixtures keep ``ThinkEngine._run`` fully offline and deterministic:

- :class:`FakeLLM` is a duck-typed :class:`~mote.common.interface.LLMClient`.
  ``aask`` returns the next queued reply (or a constant); ``aask_tool`` returns a
  pre-built :class:`~mote.router.llm.llm_response.LLMResponse`. Both record
  their calls so tests can assert which channel fired.
- :class:`FakeMemory` is a tiny in-memory :class:`MessageStore`; tests seed it
  with history so the dedup checks have something to compare against.
- ``patch_reporter`` (autouse) swaps the real :class:`ThoughtReporter` (which
  would open an LLM stream task and POST to a callback URL) for a no-op async
  context manager, so ``_run`` never touches the network or the stream queue.
"""
from __future__ import annotations

from typing import Optional

import pytest
from mote.common.const import TOOL_CALLS
from mote.common.schema import Message, UserMessage
from mote.router.llm.llm_response import LLMResponse, LLMToolCall


class FakeLLM:
    """Duck-typed LLMClient for think-engine tests."""

    def __init__(
        self,
        *,
        reply: str = "thought",
        replies: Optional[list[str]] = None,
        tool_response: Optional[LLMResponse] = None,
        model: str = "fake-model",
    ):
        self.model = model
        self._reply = reply
        self._replies = list(replies) if replies is not None else None
        self._tool_response = tool_response
        # Call logs for assertions.
        self.aask_calls: list[dict] = []
        self.aask_tool_calls: list[dict] = []
        self.format_msg_calls: list = []

    async def aask(self, msg, system_msgs=None, **kwargs) -> str:
        self.aask_calls.append({"msg": msg, "system_msgs": system_msgs, "kwargs": kwargs})
        if self._replies is not None:
            return self._replies.pop(0) if self._replies else self._reply
        return self._reply

    async def aask_tool(self, msg, system_msgs=None, tools=None, **kwargs) -> LLMResponse:
        self.aask_tool_calls.append({"msg": msg, "system_msgs": system_msgs, "tools": tools, "kwargs": kwargs})
        return self._tool_response or LLMResponse()

    def format_msg(self, messages):
        # Used by the dedup helpers when building the "summarize problem" context.
        self.format_msg_calls.append(messages)
        return messages


class FakeMemory:
    """Minimal in-memory MessageStore stand-in."""

    def __init__(self, messages: Optional[list[Message]] = None):
        self._messages: list[Message] = list(messages or [])

    def get(self, k: int = 0) -> list[Message]:
        if k <= 0:
            return list(self._messages)
        return self._messages[-k:]

    def add(self, message: Message) -> None:
        self._messages.append(message)

    def add_batch(self, messages: list[Message]) -> None:
        for m in messages:
            if m:
                self._messages.append(m)

    def delete(self, message: Message) -> None:
        if message in self._messages:
            self._messages.remove(message)


class FakeReporter:
    """No-op async context manager replacing ThoughtReporter in tests."""

    instances: list["FakeReporter"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.reports: list = []
        FakeReporter.instances.append(self)

    async def __aenter__(self) -> "FakeReporter":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def async_report(self, value, name: str = "object"):
        self.reports.append((value, name))
        return None


@pytest.fixture(autouse=True)
def patch_reporter(monkeypatch):
    """Replace ThoughtReporter so _run never opens a stream / posts a callback."""
    FakeReporter.instances.clear()
    monkeypatch.setattr("mote.think.think_engine.ThoughtReporter", FakeReporter)
    return FakeReporter


def make_tool_response(*calls, content: str = "") -> LLMResponse:
    """Build an LLMResponse from ``(id, name, arguments)`` tuples."""
    return LLMResponse(
        content=content,
        tool_calls=[LLMToolCall(id=i, name=n, arguments=a) for (i, n, a) in calls],
    )


def history_with_calls(*signatures) -> list[Message]:
    """Build memory messages each carrying TOOL_CALLS metadata.

    Each ``signature`` is a list of ``{"name":..., "args":...}`` dicts (the
    recorded-call shape ``call_signature`` reads).
    """
    msgs: list[Message] = []
    for sig in signatures:
        m = UserMessage(content="")
        m.add_metadata(TOOL_CALLS, sig)
        msgs.append(m)
    return msgs
