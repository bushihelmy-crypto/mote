#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Integration-test harness: drive a *real* Role.run end-to-end.

Unlike the per-subsystem suites (which duck-type every collaborator), these
tests wire up the real stack — ``Role`` → ``ReActLoop`` → ``ThinkEngine`` +
``ToolExecutor`` → ``ContextManager`` → session persistence — and fake only the
single external dependency: the LLM (the network boundary).

The LLM is scripted through the **native tool-use channel** (the default
``command_protocol="native"``). ``ThinkEngine`` calls ``llm.aask_tool(...)`` and
reads back an :class:`LLMResponse` carrying ``content`` + structured
``tool_calls``; the loop executes those calls against real tools. A turn that
returns *no* tool_calls is the native terminal signal (the model "finished"),
so a script ends with a plain-text turn.

Key pieces:

- :class:`ScriptedLLM` — a ``BaseLLM`` stand-in whose ``aask_tool`` replays a
  pre-baked list of turns (tool calls or terminal text), and whose ``aask``
  returns a canned string (only the dedup/ask-human override path would call
  it, which the scripts deliberately avoid).
- :class:`ScriptedRouter` — every ``route*`` entry point hands back the one
  ``ScriptedLLM`` (mirrors how ``Role`` resolves its think LLM through the
  router). Seeded into the component graph's ``router`` slot so nothing touches
  the network.
- :func:`build_role` — constructs a real ``Role`` rooted at a tmp workspace,
  with the scripted router pre-seeded.
- the ``redirect_sessions`` autouse fixture points the durable session log +
  listing at a tmp dir so ``rollout.jsonl`` never escapes into the real
  workspace.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Optional, Sequence, Union

import pytest
from mote.router.llm.llm_response import LLMResponse, LLMToolCall

# ---------------------------------------------------------------------------
# Turn scripting
# ---------------------------------------------------------------------------
#: One scripted turn is either:
#:   * a string                 -> terminal plain-text turn (no tool_calls)
#:   * a list of (name, args)   -> a tool-call turn
ToolCallSpec = tuple[str, dict]
Turn = Union[str, Sequence[ToolCallSpec]]


class ScriptedLLM:
    """A ``BaseLLM`` stand-in that replays scripted native-tool-use turns.

    ``turns`` is consumed front-to-back, one per ``aask_tool`` call. When the
    script is exhausted it falls back to a terminal empty turn so a runaway
    loop still stops instead of hanging.
    """

    def __init__(self, turns: Sequence[Turn], *, model: str = "gpt-4o", reply: str = "done"):
        self._turns: deque[Turn] = deque(turns)
        self.model = model
        self.reply = reply
        # Observability for assertions.
        self.tool_calls_seen: list[Any] = []
        self.system_msgs_seen: list[Any] = []
        self.tools_seen: list[Any] = []
        self.aask_calls: list[Any] = []
        self._call_no = 0

    async def aask_tool(self, msg, system_msgs=None, tools=None, tool_choice=None, **kwargs) -> LLMResponse:
        self.system_msgs_seen.append(system_msgs)
        self.tools_seen.append(tools)
        self.tool_calls_seen.append(msg)
        turn: Turn = self._turns.popleft() if self._turns else self.reply

        if isinstance(turn, str):
            return LLMResponse(content=turn, tool_calls=[])

        calls = []
        for name, args in turn:
            self._call_no += 1
            calls.append(LLMToolCall(id=f"call_{self._call_no}", name=name, arguments=dict(args)))
        return LLMResponse(content="", tool_calls=calls)

    async def aask(self, msg, system_msgs=None, stream=True, **kwargs) -> str:
        # Only the dedup / consecutive-limit override path calls this; the
        # scripts steer clear of it, but return something innocuous regardless.
        self.aask_calls.append(msg)
        return self.reply

    def format_msg(self, messages):
        return messages


class ScriptedRouter:
    """Stand-in for ``LLMRouter`` — every resolution returns the scripted LLM."""

    def __init__(self, llm: ScriptedLLM):
        self.llm = llm
        self.task_calls: list[str] = []

    def route_for_task(self, task: str) -> ScriptedLLM:
        self.task_calls.append(task)
        return self.llm

    def route(self, *, name=None, llm_config=None) -> ScriptedLLM:
        return self.llm

    async def aroute(self, request) -> ScriptedLLM:
        return self.llm


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def redirect_sessions(tmp_path, monkeypatch):
    """Point the durable session log + listing at a per-test tmp directory.

    ``SessionLog`` / ``list_sessions`` / ``fork`` all fall back to
    ``_default_base_dir()`` when no explicit base is given (which ``Role`` never
    passes), so patching that one function keeps every rollout under tmp.
    """
    from pathlib import Path

    import mote.session.listing as listing
    import mote.session.log as log

    base = Path(tmp_path) / ".agent_sessions"

    def _base() -> Path:
        return base

    monkeypatch.setattr(log, "_default_base_dir", _base)
    monkeypatch.setattr(listing, "_default_base_dir", _base)
    return base


@pytest.fixture
def context():
    """A real router Context (builds entirely offline, no network)."""
    from mote.router.llm.context import Context

    return Context()


def build_role(
    context,
    *,
    turns: Sequence[Turn],
    working_dir: str,
    name: str = "Tester",
    tools: Optional[list[str]] = None,
    llm_model: str = "gpt-4o",
    **schema_kwargs,
):
    """Construct a real ``Role`` wired to a :class:`ScriptedLLM`.

    The role uses the real ContextManager, ToolExecutor, ThinkEngine,
    ReActLoop and native command channel; only the router (hence the LLM) is
    faked. ``working_dir`` roots the filesystem tools at a tmp workspace.
    """
    from mote.common.schema import PermissionConfig
    from mote.roles import Role
    from mote.roles.role_schema import RoleSchema

    if tools is None:
        tools = ["Read", "Write", "Edit", "Glob", "Grep"]

    # The RoleSchema default now engages the approval engine in "default" mode
    # (every tool prompts). These tests have no interactive channel, so unless a
    # test is specifically exercising permissions, run wide-open with bypass.
    schema_kwargs.setdefault("permissions", PermissionConfig(mode="bypass"))

    schema = RoleSchema(name=name, tools=tools, **schema_kwargs)
    role = Role(role_schema=schema, context=context)
    role.state.working_dir = working_dir
    role.state.original_working_dir = working_dir
    role.state.project_root = working_dir

    llm = ScriptedLLM(turns, model=llm_model)
    role._components._graph.seed("router", ScriptedRouter(llm))
    # Expose the scripted LLM for assertions.
    role.scripted_llm = llm  # type: ignore[attr-defined]
    return role


@pytest.fixture
def make_role(context):
    """Factory fixture: ``make_role(turns=..., working_dir=..., ...)``."""

    def _factory(**kwargs):
        return build_role(context, **kwargs)

    return _factory
