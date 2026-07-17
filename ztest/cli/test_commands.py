#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the slash-command registry + builtin handlers (§2.7).

Two concerns: the :class:`CommandRegistry` dispatch mechanics (parse / alias /
unknown / empty-line → help) and the builtin handlers' contract — every handler
renders via ``ctx.notice(...)`` and delegates agent-lifecycle work to the host
surface, never touching stdout. A ``FakeCtx`` stands in for the
:class:`SessionDriver` host surface so handlers are testable in isolation.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

import pytest

from mote.cli.commands.registry import Command, CommandRegistry, default_registry


class FakeCtx:
    """Duck-typed SessionDriver host surface; records notices + lifecycle calls."""

    def __init__(self) -> None:
        self.notices: List[str] = []
        self.exited = False
        self.calls: List[Tuple[str, Any]] = []
        self.current_agent_id = "agent-0001"
        self._agents = [("agent-0001", "Assistant", "idle")]
        self._sessions: List[Any] = []
        self.switch_result: Optional[Tuple[str, str]] = ("agent-0002", "Helper")
        self.new_result: Optional[str] = "agent-0002"
        self.fork_result: Optional[str] = "agent-0003"
        self.resume_result: Tuple[bool, str] = (True, "resumed session abc12345")
        self.clear_result: int = 3
        self.agent_types: List[Tuple[str, str]] = [("Coder", "writes code"), ("Explore", "")]
        self.spawn_result: Tuple[Optional[str], str] = ("spawn-0001", "coder")

    def notice(self, text: str, level: str = "info") -> None:
        self.notices.append(text)

    def help_text(self) -> str:
        return "Commands:\n  /help  show this help\n"

    def request_exit(self) -> None:
        self.exited = True

    def active_agents(self):
        return self._agents

    def switch_agent(self, ref: str):
        self.calls.append(("switch_agent", ref))
        return self.switch_result

    def new_agent(self, name: str = "Assistant"):
        self.calls.append(("new_agent", name))
        return self.new_result

    def fork_current(self):
        self.calls.append(("fork_current", None))
        return self.fork_result

    def list_resumable_sessions(self):
        return self._sessions

    def show_sessions(self) -> int:
        self.calls.append(("show_sessions", None))
        return len(self._sessions)

    def resume_session_ref(self, ref: str):
        self.calls.append(("resume_session_ref", ref))
        return self.resume_result

    async def clear_conversation(self) -> int:
        self.calls.append(("clear_conversation", None))
        return self.clear_result

    def list_agent_types(self):
        return self.agent_types

    def spawn_agent_type(self, agent_type: str, name: str = ""):
        self.calls.append(("spawn_agent_type", (agent_type, name)))
        return self.spawn_result


@pytest.fixture
def reg():
    return default_registry()


@pytest.fixture
def ctx():
    return FakeCtx()


# --------------------------------------------------------------------------
# Registry mechanics
# --------------------------------------------------------------------------


def test_is_command_matches_registered_only(reg):
    # A registered command (incl. leading whitespace / alias / arg) → command.
    assert reg.is_command("/help")
    assert reg.is_command("   /exit")
    assert reg.is_command("/q")  # alias
    assert reg.is_command("/agent 1")  # command + arg
    # Non-slash prose is never a command.
    assert not reg.is_command("hello")


def test_is_command_slash_prefix_without_match_is_conversation(reg):
    # A ``/``-prefixed line whose first token matches no command is ordinary
    # conversation (a path, prose, an unknown word) — NOT a failed command.
    assert not reg.is_command("/home/longert/file.py")
    assert not reg.is_command("/usr/bin/python")
    assert not reg.is_command("/nonexistent do the thing")
    assert not reg.is_command("/")  # bare slash matches nothing
    assert not reg.is_command("/ hello")  # slash + space + prose


def test_builtins_are_registered(reg):
    names = {c.name for c in reg.commands()}
    assert {"help", "exit", "agents", "agent", "new", "fork", "sessions", "resume", "clear"} <= names


def test_alias_resolution(reg):
    assert reg.resolve("q").name == "exit"
    assert reg.resolve("h").name == "help"
    assert reg.resolve("?").name == "help"
    assert reg.resolve("ls").name == "sessions"
    assert reg.resolve("switch").name == "agent"
    assert reg.resolve("reset").name == "clear"


def test_resolve_unknown_returns_none(reg):
    assert reg.resolve("nope") is None


def test_help_text_lists_commands(reg):
    text = reg.help_text()
    assert text.startswith("Commands:")
    assert "/help" in text


@pytest.mark.asyncio
async def test_unknown_command_notices_hint(reg, ctx):
    await reg.handle(ctx, "/bogus")
    assert any("Unknown command: /bogus" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_empty_command_dispatches_help(reg, ctx):
    await reg.handle(ctx, "/")
    # help handler renders ctx.help_text()
    assert any("Commands:" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_handle_parses_name_and_arg(ctx):
    captured = {}

    async def handler(c, arg):
        captured["arg"] = arg
        c.notice("ran")

    reg = CommandRegistry()
    reg.register(Command(name="echo", handler=handler))
    await reg.handle(ctx, "/echo hello world")
    assert captured["arg"] == "hello world"
    assert "ran" in ctx.notices


# --------------------------------------------------------------------------
# Builtin handler contracts
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_command(reg, ctx):
    await reg.handle(ctx, "/help")
    assert any("Commands:" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_exit_command(reg, ctx):
    await reg.handle(ctx, "/exit")
    assert ctx.exited is True


@pytest.mark.asyncio
async def test_agents_lists_with_current_marker(reg, ctx):
    await reg.handle(ctx, "/agents")
    out = "\n".join(ctx.notices)
    assert "Agents:" in out
    assert "*" in out  # current agent marker
    assert "agent-00" in out


@pytest.mark.asyncio
async def test_agent_switch_success(reg, ctx):
    await reg.handle(ctx, "/agent 1")
    assert ("switch_agent", "1") in ctx.calls
    assert any("switched to Helper" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_agent_switch_no_arg_shows_usage(reg, ctx):
    await reg.handle(ctx, "/agent")
    assert any("usage:" in n for n in ctx.notices)
    assert ("switch_agent", "") not in ctx.calls


@pytest.mark.asyncio
async def test_agent_switch_not_found(reg, ctx):
    ctx.switch_result = None
    await reg.handle(ctx, "/agent zzz")
    assert any("no agent matching" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_new_agent(reg, ctx):
    await reg.handle(ctx, "/new Bob")
    assert ("new_agent", "Bob") in ctx.calls
    assert any("created agent" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_new_agent_default_name(reg, ctx):
    await reg.handle(ctx, "/new")
    assert ("new_agent", "Assistant") in ctx.calls


@pytest.mark.asyncio
async def test_fork(reg, ctx):
    await reg.handle(ctx, "/fork")
    assert ("fork_current", None) in ctx.calls
    assert any("forked into agent" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_fork_failure(reg, ctx):
    ctx.fork_result = None
    await reg.handle(ctx, "/fork")
    assert any("cannot fork" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_sessions_empty(reg, ctx):
    await reg.handle(ctx, "/sessions")
    assert ("show_sessions", None) in ctx.calls
    assert any("no resumable sessions" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_sessions_nonempty_delegates_to_structured(reg, ctx):
    # Non-empty: the handler delegates rendering to the structured
    # ``show_sessions`` (SessionListShown), emitting no fallback notice.
    ctx._sessions = [object(), object()]
    await reg.handle(ctx, "/sessions")
    assert ("show_sessions", None) in ctx.calls
    assert not any("no resumable sessions" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_resume_no_arg_usage(reg, ctx):
    await reg.handle(ctx, "/resume")
    assert any("usage:" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_resume_delegates_and_reports(reg, ctx):
    await reg.handle(ctx, "/resume 0")
    assert ("resume_session_ref", "0") in ctx.calls
    assert any("resumed session abc12345" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_clear_delegates_and_reports(reg, ctx):
    await reg.handle(ctx, "/clear")
    assert ("clear_conversation", None) in ctx.calls
    assert any("cleared conversation (3 messages)" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_clear_alias_reset(reg, ctx):
    await reg.handle(ctx, "/reset")
    assert ("clear_conversation", None) in ctx.calls


# --------------------------------------------------------------------------
# Typed agent commands
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_types_lists_registered(reg, ctx):
    await reg.handle(ctx, "/agent-types")
    out = "\n".join(ctx.notices)
    assert "Agent types:" in out
    assert "Coder: writes code" in out
    assert "- Explore" in out  # description-less type still listed


@pytest.mark.asyncio
async def test_agent_types_empty(reg, ctx):
    ctx.agent_types = []
    await reg.handle(ctx, "/agent-types")
    assert any("no agent types registered" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_agent_types_alias_types(reg, ctx):
    await reg.handle(ctx, "/types")
    assert any("Agent types:" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_spawn_known_type(reg, ctx):
    await reg.handle(ctx, "/spawn coder")
    assert ("spawn_agent_type", ("coder", "")) in ctx.calls
    assert any("spawned coder (spawn-00" in n for n in ctx.notices)


@pytest.mark.asyncio
async def test_spawn_with_name(reg, ctx):
    await reg.handle(ctx, "/spawn coder Bob")
    assert ("spawn_agent_type", ("coder", "Bob")) in ctx.calls


@pytest.mark.asyncio
async def test_spawn_no_arg_usage(reg, ctx):
    await reg.handle(ctx, "/spawn")
    assert any("usage:" in n for n in ctx.notices)
    assert not any(c[0] == "spawn_agent_type" for c in ctx.calls)


@pytest.mark.asyncio
async def test_spawn_unknown_type_reports_failure(reg, ctx):
    ctx.spawn_result = (None, "unknown/unavailable agent type 'nope'")
    await reg.handle(ctx, "/spawn nope")
    assert any("unknown/unavailable" in n for n in ctx.notices)
