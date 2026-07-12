#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures for the parser (command-channel) test suite.

The two channels (:class:`~mote.parser.native_channel.NativeToolChannel`,
:class:`~mote.parser.xml_channel.XmlCommandChannel`) only touch three
collaborators, all of which are duck-typed here so the tests stay offline:

- :class:`FakeThinkEngine` exposes the slice the channels read from a finished
  think round: ``done`` / ``join()`` (the channels block on the task being done
  before reading) and ``result`` (a real :class:`ThinkResult`). ``join`` flips
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

from mote.common.schema import Message, ThinkResult


class FakeThinkEngine:
    """Duck-typed BaseThinkEngine exposing only what the channels read."""

    def __init__(self, *, content: str = "", tool_calls: Optional[list[dict]] = None, done: bool = True):
        self.result = ThinkResult(content=content, tool_calls=tool_calls)
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

    def get_native_tool_specs(self, provider: str) -> Optional[list[dict]]:
        self.provider_calls.append(provider)
        return self._specs


def executed_command(
    *,
    id: Optional[str] = None,
    name: str = "Read",
    args: Optional[dict] = None,
    output: str = "ok",
    success: bool = True,
    images: Optional[list[str]] = None,
    pdfs: Optional[list[str]] = None,
    resource_path: Optional[str] = None,
) -> dict[str, Any]:
    """Build one entry of the ``executed`` list that ``record_turn`` consumes."""
    cmd: dict[str, Any] = {
        "id": id,
        "name": name,
        "args": args or {},
        "output": output,
        "success": success,
    }
    if images is not None:
        cmd["images"] = images
    if pdfs is not None:
        cmd["pdfs"] = pdfs
    if resource_path is not None:
        cmd["resource_path"] = resource_path
    return cmd


async def collect(channel, think_engine, valid_names: set[str]) -> list[dict]:
    """Drain ``channel.iter_commands`` into a list."""
    return [cmd async for cmd in channel.iter_commands(think_engine, valid_names)]


class _LLMConfig:
    """Tiny object mimicking an LLMConfig for ``infer_native_tool_provider``.

    ``infer_native_tool_provider`` resolves the envelope from the transport
    (``api_type`` / ``base_url``), so the stub carries both; ``model`` is kept
    for back-compat but no longer affects the result.
    """

    def __init__(self, model=None, api_type=None, base_url=""):
        from mote.common.config.config.llm_config import LLMType

        self.model = model
        self.api_type = api_type if api_type is not None else LLMType.OPENAI
        self.base_url = base_url
