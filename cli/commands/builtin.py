#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Builtin slash commands — ported from ``cli/commands.py`` to objects (§2.7).

Each handler is ``async (ctx, arg)`` and renders via ``ctx.notice(...)`` (a
``Notice`` ``ViewEvent`` → every consumer), never raw stdout. The agent-lifecycle
work delegates to the :class:`SessionDriver` host surface, keeping handlers thin
parse/format shells (trivially unit-testable against a fake ctx).
"""

from __future__ import annotations

from typing import Any

from mote.cli.commands.registry import register_command


@register_command("help", help="show this help", aliases=("h", "?"))
async def cmd_help(ctx: Any, _arg: str) -> None:
    ctx.notice(ctx.help_text())


@register_command("exit", help="leave the session", aliases=("quit", "q"))
async def cmd_exit(ctx: Any, _arg: str) -> None:
    ctx.request_exit()


@register_command("agents", help="list agents in this session")
async def cmd_agents(ctx: Any, _arg: str) -> None:
    agents = ctx.active_agents()
    if not agents:
        ctx.notice("(no agents)\n")
        return
    current = ctx.current_agent_id
    lines = ["Agents:"]
    for i, (agent_id, name, status) in enumerate(agents):
        marker = "*" if agent_id == current else " "
        lines.append(f" {marker}[{i}] {name}  {agent_id[:8]}  ({status})")
    lines.append("")
    ctx.notice("\n".join(lines))


@register_command("agent", help="switch active agent: /agent <index|session-id|name>", aliases=("switch",))
async def cmd_agent(ctx: Any, arg: str) -> None:
    if not arg:
        ctx.notice("usage: /agent <index|session-id|name>\n")
        return
    result = ctx.switch_agent(arg)
    if result is None:
        ctx.notice(f"no agent matching '{arg}'.\n")
        return
    agent_id, name = result
    ctx.notice(f"switched to {name} ({agent_id[:8]}).\n")


@register_command("new", help="spawn a fresh agent and switch: /new [name]")
async def cmd_new(ctx: Any, arg: str) -> None:
    agent_id = ctx.new_agent(arg or "Assistant")
    if agent_id is None:
        ctx.notice("cannot create a new agent here.\n")
        return
    ctx.notice(f"created agent {agent_id[:8]} (active).\n")


@register_command("agent-types", help="list available agent types", aliases=("types",))
async def cmd_agent_types(ctx: Any, _arg: str) -> None:
    types = ctx.list_agent_types()
    if not types:
        ctx.notice("(no agent types registered)\n")
        return
    lines = ["Agent types:"]
    for name, desc in types:
        lines.append(f" - {name}: {desc}" if desc else f" - {name}")
    lines.append("")
    ctx.notice("\n".join(lines))


@register_command("spawn", help="spawn a typed agent: /spawn <type> [name]")
async def cmd_spawn(ctx: Any, arg: str) -> None:
    if not arg:
        ctx.notice("usage: /spawn <type> [name]\n")
        return
    parts = arg.split(maxsplit=1)
    agent_type = parts[0]
    name = parts[1] if len(parts) > 1 else ""
    session_id, result = ctx.spawn_agent_type(agent_type, name)
    if session_id is None:
        ctx.notice(f"{result}\n")
        return
    ctx.notice(f"spawned {result} ({session_id[:8]}).\n", level="success")


@register_command("fork", help="fork the current session into a new agent")
async def cmd_fork(ctx: Any, _arg: str) -> None:
    agent_id = ctx.fork_current()
    if agent_id is None:
        ctx.notice("cannot fork the current session.\n")
        return
    ctx.notice(f"forked into agent {agent_id[:8]} (active).\n")


@register_command("sessions", help="list resumable sessions", aliases=("list", "ls"))
async def cmd_sessions(ctx: Any, _arg: str) -> None:
    # The driver emits a structured ``SessionListShown`` (every consumer renders
    # it natively — terminal table / structured array), returning the row count so
    # the handler can still surface the empty case as a plain notice.
    if ctx.show_sessions() == 0:
        ctx.notice("(no resumable sessions)\n")


@register_command("resume", help="resume a session: /resume <index|session-id>")
async def cmd_resume(ctx: Any, arg: str) -> None:
    if not arg:
        ctx.notice("usage: /resume <index|session-id>\n")
        return
    ok, message = ctx.resume_session_ref(arg)
    ctx.notice(message + "\n")


@register_command("clear", help="clear the conversation (history + transcript)", aliases=("reset",))
async def cmd_clear(ctx: Any, _arg: str) -> None:
    cleared = ctx.clear_conversation()
    ctx.notice(f"cleared conversation ({cleared} messages).\n", level="success")


__all__ = [
    "cmd_help",
    "cmd_exit",
    "cmd_agents",
    "cmd_agent",
    "cmd_new",
    "cmd_agent_types",
    "cmd_spawn",
    "cmd_fork",
    "cmd_sessions",
    "cmd_resume",
    "cmd_clear",
]
