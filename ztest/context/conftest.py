#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures for the ``mote.runtime.context`` test suite.

The package owns the two *history-level* context-management scopes:

- microcompact — fold old tool-result bodies in place (cheap, no LLM).
- autocompact — summarize+rebuild the stored history when it nears the model's
  window (expensive, one LLM call).

These fixtures keep both scopes fully offline and deterministic:

- :class:`FakeLLM` is the duck-typed summarizer autocompact needs (``async aask``
  + a ``model`` attribute). It returns a queued / constant summary and records
  every call, and can be told to raise so the circuit-breaker path is testable.
- The message builders produce the exact Mote message shapes the algorithms
  read: an assistant turn carries ``metadata[TOOL_CALLS]`` (a list of
  ``{id, name, args}``), and the matching tool-result turn carries
  ``metadata[TOOL_CALL_ID]`` with its text in ``.content``.
- ``force_autocompact_threshold`` patches ``budget.autocompact_threshold``
  to a tiny value so autocompact fires on a handful of short messages instead of
  the ~167k tokens the real window math would demand.
"""
from __future__ import annotations

from typing import Optional

import pytest

from mote.contracts.constants.messages import TOOL_CALL_ID, TOOL_CALLS
from mote.contracts.schema import AIMessage, Message, UserMessage

# Tool names the compaction tests treat as reconstructable (fold/clear-safe).
# Production derives this from the live executor (each tool self-declares via its
# ``reconstructable`` ClassVar); the tests inject this fixed set explicitly since
# there is no longer a hardcoded default in the context layer.
# Includes Edit's aliases (``Write``/``write``/``Update``) because production's
# ``reconstructable_names()`` lists every name a reconstructable tool routes under,
# so a call recording the raw alias still lands in a reconstructable segment.
COMPACTABLE = frozenset({"Read", "Bash", "Grep", "Glob", "Write", "write", "Edit", "Update"})


class FakeLLM:
    """Duck-typed summarizer for autocompact tests.

    Implements only what ``autocompact`` calls: ``async aask(msg, system_msgs=,
    stream=)`` plus a ``model`` attribute (used as the token-math fallback). It
    records each call's ``msg`` / ``system_msgs`` so tests can assert that the
    head was summarized with the compact prompt.
    """

    def __init__(
        self,
        *,
        summary: str = "<summary>done</summary>",
        raise_exc: Optional[Exception] = None,
        model: str = "fake-model",
    ):
        self.model = model
        self._summary = summary
        self._raise = raise_exc
        self.aask_calls: list[dict] = []

    async def aask(self, msg=None, system_msgs=None, stream=True, **kwargs) -> str:
        self.aask_calls.append({"msg": msg, "system_msgs": system_msgs, "stream": stream, "kwargs": kwargs})
        if self._raise is not None:
            raise self._raise
        return self._summary


def text_msg(content: str, *, role: str = "user") -> Message:
    """A plain (no tool metadata) message."""
    if role == "assistant":
        return AIMessage(content=content)
    return UserMessage(content=content)


def tool_call_msg(call_id: str, name: str, *, content: str = "", args: dict | None = None) -> AIMessage:
    """An assistant turn that invoked tool ``name`` with id ``call_id``."""
    m = AIMessage(content=content)
    m.add_metadata(TOOL_CALLS, [{"id": call_id, "name": name, "args": args or {}}])
    return m


def tool_result_msg(call_id: str, content: str) -> UserMessage:
    """The tool-result turn paired to ``call_id`` (text lives in ``content``)."""
    m = UserMessage(content=content)
    m.add_metadata(TOOL_CALL_ID, call_id)
    return m


def tool_pair(call_id: str, name: str, result: str) -> list[Message]:
    """An assistant tool-call turn followed by its tool-result turn."""
    return [tool_call_msg(call_id, name), tool_result_msg(call_id, result)]


def make_pairs(n: int, *, name: str = "Read", result: str = "x" * 200, start: int = 0) -> list[Message]:
    """``n`` consecutive (call, result) pairs for tool ``name``."""
    msgs: list[Message] = []
    for i in range(start, start + n):
        msgs += tool_pair(f"id-{i}", name, result)
    return msgs


@pytest.fixture
def force_autocompact_threshold(monkeypatch):
    """Make autocompact fire on tiny inputs.

    The real autocompact threshold is ~167k tokens (200k window minus reserves),
    which is impractical to reach with test-sized histories. ``budget``'s
    ``evaluate`` looks up ``autocompact_threshold`` by module-level name at call
    time, so patching it here forces ``should_autocompact`` True for any
    non-trivial history while leaving the rest of the real math intact.
    """
    from mote.runtime.context import budget

    monkeypatch.setattr(budget, "autocompact_threshold", lambda model: 1)
    return budget
