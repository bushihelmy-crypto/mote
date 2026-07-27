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

from mote.contracts.fileops import FileOperationError
from mote.product.cli.commands.core import Command, command_definition, register_command
from mote.product.i18n import current_locale
from mote.product.i18n import keys as K
from mote.product.i18n import locales, negotiate_and_set, set_locale, t


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
    agent_id = await ctx.fork_current()
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
    cleared = await ctx.clear_conversation()
    ctx.notice(f"cleared conversation ({cleared} messages).\n", level="success")


@register_command("rewind", help="list/rollback file checkpoints: /rewind [index]")
async def cmd_rewind(ctx: Any, arg: str) -> None:
    """List whole-tree checkpoints, or roll the working tree back to one.

    No arg → list every captured user-turn checkpoint (index, preview, time).
    ``/rewind <index>`` → restore the working tree to that checkpoint (the
    current state is auto-saved first, so the rewind is itself reversible).
    """
    arg = arg.strip()
    if not arg:
        entries = ctx.list_checkpoints()
        if not entries:
            ctx.notice("(no checkpoints — /rewind needs a git-backed workspace)\n")
            return
        lines = ["Checkpoints:"]
        for e in entries:
            preview = (e.prompt_preview or "").replace("\n", " ").strip()
            ts = (e.ts or "")[:19]
            lines.append(f" [{e.index}] {ts}  {preview}" if preview else f" [{e.index}] {ts}")
        lines.append("\nRewind with: /rewind <index>")
        ctx.notice("\n".join(lines) + "\n")
        return
    if not arg.isdigit():
        ctx.notice("usage: /rewind [index]\n")
        return
    try:
        result = await ctx.rewind_to(int(arg))
    except FileOperationError as exc:
        ctx.notice(f"rewind failed: {exc}\n", level="error")
        return
    if result is None:
        ctx.notice(f"could not rewind to checkpoint {arg}.\n", level="error")
        return
    ctx.notice(f"rewound working tree to checkpoint [{result.target.index}].\n", level="success")
    external = getattr(result, "external", None)
    if external:
        lines = ["warning: overwrote files changed since that turn (outside the agent):"]
        lines += [f"  - {path}" for path in external]
        ctx.notice("\n".join(lines) + "\n", level="warning")


@register_command("usage", help="show session cost + provider rate-limit quota", aliases=("cost",))
async def cmd_usage(ctx: Any, _arg: str) -> None:
    """Render the session's accumulated cost and the latest provider rate limits.

    Both are read off the shared router context: ``cost_manager`` (spend) and
    ``rate_limit_tracker`` (the quota captured from each response's
    ``*-ratelimit-*`` headers). Rate limits read ``(none reported yet)`` until
    the first provider response of the session lands.
    """
    ctx.notice(ctx.usage_report() + "\n")


@register_command("lang", help="show or switch the display language: /lang [auto|en|zh]")
async def cmd_lang(ctx: Any, arg: str) -> None:
    """Show the active display language, or switch it (``auto`` re-reads the env).

    New output + the status bar pick up the new locale immediately (both render
    through ``t()``); already-scrolled history keeps its prior wording.
    """
    codes = ", ".join(locales())
    requested = arg.strip()
    if not requested:
        ctx.notice(t(K.LANG_CURRENT, code=current_locale().code) + "\n")
        ctx.notice(t(K.LANG_AVAILABLE, codes=codes) + "\n")
        return
    lowered = requested.lower()
    if lowered == "auto":
        loc = negotiate_and_set(config_language="auto")
    elif lowered in locales():
        loc = set_locale(lowered)
    else:
        ctx.notice(t(K.LANG_UNKNOWN, code=requested, codes=codes) + "\n")
        return
    ctx.notice(t(K.LANG_SWITCHED, code=loc.code) + "\n", level="success")


BUILTIN_COMMANDS: tuple[Command, ...] = tuple(
    command_definition(handler)
    for handler in (
        cmd_help,
        cmd_exit,
        cmd_agents,
        cmd_agent,
        cmd_new,
        cmd_agent_types,
        cmd_spawn,
        cmd_fork,
        cmd_sessions,
        cmd_resume,
        cmd_clear,
        cmd_rewind,
        cmd_usage,
        cmd_lang,
    )
)


__all__ = [
    "BUILTIN_COMMANDS",
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
    "cmd_rewind",
    "cmd_usage",
    "cmd_lang",
]
