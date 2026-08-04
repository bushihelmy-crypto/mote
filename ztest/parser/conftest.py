#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures for the parser (command-channel) test suite.

The two channels (:class:`~mote.kernel.commands.native.NativeToolChannel`,
:class:`~mote.kernel.commands.xml.channel.XmlCommandChannel`) only touch three
collaborators, all of which are duck-typed here so the tests stay offline:

- :class:`FakeThinkEngine` exposes the slice the channels read from a finished
  think round: ``done`` / ``join()`` (the channels block on the task being done
  before reading) and ``result`` (a real :class:`InferenceResult`). ``join`` flips
  ``done`` and counts calls so a test can assert whether the channel awaited it.
- :class:`FakeMemory` is a tiny :class:`MessageStore`; ``record_turn`` appends to
  it and tests inspect the recorded messages + their metadata.
- :class:`FakeExecutor` records the ``provider`` ``tool_specs`` asks for and
  returns a canned spec list.

``executed_command`` builds the per-command dict ``record_turn`` consumes.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from mote.contracts.conversation import Message
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.invocation import CanonicalToolCall
from mote.kernel.commands.contracts import ExecutedCommand


class FakeThinkEngine:
    """Duck-typed BaseInferenceEngine exposing only what the channels read."""

    def __init__(
        self,
        *,
        content: str = "",
        tool_calls: Optional[list[CanonicalToolCall]] = None,
        done: bool = True,
    ):
        self.result = InferenceResult(content=content, tool_calls=None if tool_calls is None else tuple(tool_calls))
        self.done = done
        self.join_calls = 0

    async def join(self) -> None:
        self.join_calls += 1
        self.done = True


class FakeMemory:
    """Minimal in-memory MessageStore stand-in that records added messages."""

    def __init__(self, messages: Optional[list[Message]] = None):
        self.messages: list[Message] = list(messages or [])

    async def add(self, message: Message) -> None:
        self.messages.append(message)

    def get(self, k: int = 0) -> list[Message]:
        if k <= 0:
            return list(self.messages)
        return self.messages[-k:]


class FakeExecutor:
    """Stand-in for the tool executor's native-spec provider."""

    def __init__(self, specs: Optional[list[dict]] = None):
        self._specs = specs
        self.provider_calls: list[str] = []

    def native_tool_specs(self, provider: str) -> Optional[list[dict]]:
        self.provider_calls.append(provider)
        return self._specs

    def canonical_tool_specs(self, *, include_hidden: bool = True) -> Optional[list[dict]]:
        if include_hidden:
            return self._specs
        return [spec for spec in self._specs or [] if not spec.get("defer_loading")]


def executed_command(
    *,
    id: Optional[str] = None,
    name: str = "Read",
    args: Optional[dict] = None,
    output: str = "ok",
    success: bool = True,
    media: Optional[list[Any]] = None,
    resource_path: Optional[str] = None,
) -> ExecutedCommand:
    """Build one entry of the ``executed`` list that ``record_turn`` consumes."""
    cmd = ExecutedCommand(
        action_id=id,
        name=name,
        arguments=args or {},
        output=output,
        success=success,
    )
    if media is not None:
        cmd.media = media
    if resource_path is not None:
        cmd.resource_path = resource_path
    return cmd


async def collect(channel, inference_engine, valid_names: set[str]) -> list[dict]:
    """Project the canonical ModelTurn tool actions into assertion records."""
    from mote.contracts.model.turn import ToolCallAction

    turn = await channel.model_turn(inference_engine.result)
    return [
        {
            "id": action.action_id or None,
            "command_name": action.name,
            "args": action.arguments,
            "status": "running",
            "error_msg": "",
        }
        for action in turn.actions
        if isinstance(action, ToolCallAction) and (not valid_names or action.name in valid_names)
    ]


async def apply_projection(memory: FakeMemory, projection) -> None:
    resolved = await projection
    memory.messages.extend(resolved.messages)


class _LLMConfig:
    """Tiny LLMConfig-shaped object retained by parser fixture call sites."""

    def __init__(self, model=None, api_type=None, base_url=""):
        from mote.contracts.config.model.llm import LLMType

        self.model = model
        self.api_type = api_type if api_type is not None else LLMType.OPENAI
        self.base_url = base_url
