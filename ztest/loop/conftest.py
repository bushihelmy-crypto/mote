#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures + duck-typed fakes for the ReActLoop test suite.

The loop (:class:`metagpt.loop.react_loop.ReActLoop`) is deliberately
role-agnostic: it drives a handful of injected collaborators behind narrow
interfaces and reads/writes the shared ``active`` signal via two plain
callables. None of those collaborators need a network, so every one is
duck-typed here and the tests stay fully offline.

Collaborators the loop touches (and the slice each fake implements):

- ``think_engine``  : ``async start(...)`` + ``result.content`` + ``async join()``
  (see :class:`FakeThinkEngine`).
- ``command_channel``: ``iter_commands(engine, valid_names)`` (async gen),
  ``record_turn(memory, content, executed)`` and ``is_terminal(engine)``
  (see :class:`FakeChannel`).
- ``executor``      : ``async run_command(name, args, result_id=None)`` returning
  a :class:`FakeResult` (``output`` / ``success`` / ``images`` / ``pdfs``).
- ``memory``        : a tiny ``MessageStore`` (``get`` / ``add`` / ``add_batch``).
- ``context_provider``: ``loop_context()`` / ``async prepare()`` /
  ``async resolve_llm(messages)`` (see :class:`FakeContextProvider`).
- ``is_active`` / ``set_active`` : read/write a shared bool holder.
- ``get_bg_pool``  : returns a :class:`FakeBgPool` or ``None``.

``make_loop`` wires a fully-defaulted loop and hands back both the loop and the
fakes so a test can pre-seed inputs and assert on recorded calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest

from metagpt.common.base import LoopContext
from metagpt.common.schema import Message, UserMessage
from metagpt.loop.react_loop import ReActLoop
from metagpt.roles.context_provider.request import ThinkRequest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    """Minimal stand-in for a finished ThinkResult (only ``content`` read)."""

    def __init__(self, content: str = ""):
        self.content = content


class FakeThinkEngine:
    """Duck-typed BaseThinkEngine exposing only what the loop reads."""

    def __init__(self, *, content: str = ""):
        self.result = _Result(content)
        self.start_calls: list[dict] = []
        self.join_calls = 0

    async def start(self, req, system_prompt, tool_specs=None, *, llm):
        self.start_calls.append(
            {
                "req": req,
                "system_prompt": system_prompt,
                "tool_specs": tool_specs,
                "llm": llm,
            }
        )

    async def join(self) -> None:
        self.join_calls += 1


class FakeChannel:
    """Duck-typed CommandChannel.

    ``commands`` is the list ``iter_commands`` yields (command IR dicts shaped
    ``{"id", "command_name", "args"}``). ``terminal`` drives ``is_terminal``.
    ``record_turn`` records ``(content, executed)`` tuples for assertion.
    """

    def __init__(self, *, commands: Optional[list[dict]] = None, terminal: bool = False):
        self.commands = list(commands or [])
        self.terminal = terminal
        self.recorded_turns: list[tuple[str, list[dict]]] = []
        self.iter_calls: list[set] = []

    async def iter_commands(self, think_engine, valid_names):
        self.iter_calls.append(set(valid_names))
        for cmd in self.commands:
            yield cmd

    async def record_turn(self, memory, content, executed) -> None:
        self.recorded_turns.append((content, list(executed)))

    async def is_terminal(self, think_engine) -> bool:
        return self.terminal

    def react_result(self, outputs: str) -> str:
        # Mirror the base default (plain outputs); the loop's content is then
        # assertable against the joined command outputs / no-commands notice.
        return outputs


@dataclass
class FakeResult:
    """Stand-in for the executor's ToolResult return."""

    output: str = "ok"
    success: bool = True
    images: list = field(default_factory=list)
    pdfs: list = field(default_factory=list)
    terminate: bool = False
    retention: str | None = None


class FakeExecutor:
    """Duck-typed BaseToolExecutor.

    ``results`` maps a command name to the :class:`FakeResult` ``run_command``
    returns; anything missing falls back to ``default``.
    """

    def __init__(self, *, results: Optional[dict[str, FakeResult]] = None, default: Optional[FakeResult] = None):
        self.results = results or {}
        self.default = default or FakeResult()
        self.calls: list[dict] = []

    async def run_command(self, name, args, result_id=None):
        self.calls.append({"name": name, "args": args, "result_id": result_id})
        return self.results.get(name, self.default)


class FakeMemory:
    """Minimal in-memory MessageStore stand-in."""

    def __init__(self, messages: Optional[list[Message]] = None):
        self.messages: list[Message] = list(messages or [])
        self.add_batch_calls: list[list[Message]] = []

    def get(self, k: int = 0) -> list[Message]:
        if k <= 0:
            return list(self.messages)
        return self.messages[-k:]

    async def add(self, message: Message) -> None:
        self.messages.append(message)

    async def add_batch(self, messages) -> None:
        msgs = [m for m in messages if m is not None]
        self.add_batch_calls.append(list(msgs))
        self.messages.extend(msgs)


class FakeLLM:
    """Duck-typed LLMClient — only ``aask`` is exercised by the loop."""

    def __init__(self, reply: str = "llm-question"):
        self.reply = reply
        self.aask_calls: list = []

    async def aask(self, msg, *args, **kwargs):
        self.aask_calls.append(msg)
        return self.reply


class FakeContextProvider:
    """Duck-typed BaseContextProvider.

    Hands back the static :class:`LoopContext`, a canned :class:`ThinkRequest`
    per ``prepare()`` and a :class:`FakeLLM` per ``resolve_llm()``.
    """

    def __init__(self, ctx: LoopContext, *, think_request: Optional[ThinkRequest] = None, llm: Optional[FakeLLM] = None):
        self._ctx = ctx
        self._think_request = think_request or ThinkRequest(
            req=[UserMessage("hi")], system_prompt="sys", tool_specs=["spec"]
        )
        self.llm = llm or FakeLLM()
        self.prepare_calls = 0
        self.resolve_calls: list = []

    def loop_context(self) -> LoopContext:
        return self._ctx

    async def prepare(self) -> ThinkRequest:
        self.prepare_calls += 1
        return self._think_request

    async def resolve_llm(self, messages=None):
        self.resolve_calls.append(messages)
        return self.llm


class FakeBgPool:
    """Duck-typed BackgroundPool.

    ``pending`` is the number of times ``has_pending()`` should report busy;
    each ``wait_any()`` decrements it (so a parked loop eventually drains).
    """

    def __init__(self, pending: int = 0):
        self.pending = pending
        self.wait_any_calls = 0

    def has_pending(self) -> bool:
        return self.pending > 0

    @property
    def pending_count(self) -> int:
        return self.pending

    async def wait_any(self) -> None:
        self.wait_any_calls += 1
        if self.pending > 0:
            self.pending -= 1


# ---------------------------------------------------------------------------
# Builders / fixtures
# ---------------------------------------------------------------------------


def make_loop_context(**overrides) -> LoopContext:
    """Build a :class:`LoopContext` with sensible test defaults."""
    from metagpt.common.schema import MessageQueue

    params: dict[str, Any] = dict(
        max_react_loop=5,
        max_consecutive_react_limit=3,
        memory_k=10,
        name="Alice",
        display_name="Alice(Tester)",
        tools=["Read", "AskUserQuestion"],
        msg_buffer=MessageQueue(),
        watch=set(),
        enable_memory=True,
        observe_all=True,
    )
    params.update(overrides)
    return LoopContext(**params)


@dataclass
class LoopBundle:
    """The loop plus every fake wired into it (for inspection in tests)."""

    loop: ReActLoop
    ctx: LoopContext
    think_engine: FakeThinkEngine
    channel: FakeChannel
    executor: FakeExecutor
    memory: FakeMemory
    provider: FakeContextProvider
    active: list  # single-element bool holder
    bg_pool_holder: list  # single-element [FakeBgPool | None]
    reported: list  # think results published via report_think_result

    @property
    def buffer(self):
        return self.ctx.msg_buffer


@pytest.fixture
def make_loop():
    """Factory: build a fully-wired :class:`ReActLoop` + its fakes.

    Pass keyword overrides for any collaborator or for ``LoopContext`` fields
    (anything not a known collaborator is forwarded to ``make_loop_context``).
    """

    def _factory(
        *,
        ctx: Optional[LoopContext] = None,
        think_engine: Optional[FakeThinkEngine] = None,
        channel: Optional[FakeChannel] = None,
        executor: Optional[FakeExecutor] = None,
        memory: Optional[FakeMemory] = None,
        provider: Optional[FakeContextProvider] = None,
        active: bool = True,
        bg_pool: Optional[FakeBgPool] = None,
        **ctx_overrides,
    ) -> LoopBundle:
        ctx = ctx or make_loop_context(**ctx_overrides)
        think_engine = think_engine or FakeThinkEngine()
        channel = channel or FakeChannel()
        executor = executor or FakeExecutor()
        memory = memory or FakeMemory()
        provider = provider or FakeContextProvider(ctx)

        active_holder = [active]
        bg_holder = [bg_pool]
        reported: list = []

        def is_active() -> bool:
            return active_holder[0]

        def set_active(v: bool) -> None:
            active_holder[0] = v

        def get_bg_pool():
            return bg_holder[0]

        def report_think_result(result) -> None:
            reported.append(result)

        loop = ReActLoop(
            think_engine=think_engine,
            command_channel=channel,
            executor=executor,
            memory=memory,
            context_provider=provider,
            is_active=is_active,
            set_active=set_active,
            get_bg_pool=get_bg_pool,
            report_think_result=report_think_result,
        )
        return LoopBundle(
            loop=loop,
            ctx=ctx,
            think_engine=think_engine,
            channel=channel,
            executor=executor,
            memory=memory,
            provider=provider,
            active=active_holder,
            bg_pool_holder=bg_holder,
            reported=reported,
        )

    return _factory
