#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Slash-command dispatch for the interactive REPL.

A line beginning with ``/`` is a command, not a turn. Commands are thin: they
parse + format, then delegate the real work (agent switching, session
resume/fork, control-plane wiring) to high-level :class:`~metagpt.cli.repl.Repl`
methods. This keeps the dispatcher trivially unit-testable against a fake repl
surface, while the integration wiring is exercised through the REPL's own tests.

Supported commands::

    /help                  show this help
    /exit, /quit           leave the REPL
    /agents                list agents in this session's control plane
    /agent, /switch <ref>  switch the active agent (index | session-id | name)
    /new [name]            spawn a fresh agent and switch to it
    /fork                  fork the current session into a new agent + switch
    /sessions, /list       list resumable sessions (newest first)
    /resume <ref>          resume a session (index from /sessions | session-id)
"""

from __future__ import annotations

from typing import Any

# Canonical command -> one-line help. The handler is ``_cmd_<name>``. Aliases map
# onto a canonical name below.
_COMMANDS = {
    "help": "show this help",
    "exit": "leave the REPL",
    "agents": "list agents in this session",
    "agent": "switch active agent: /agent <index|session-id|name>",
    "new": "spawn a fresh agent and switch: /new [name]",
    "fork": "fork the current session into a new agent",
    "sessions": "list resumable sessions",
    "resume": "resume a session: /resume <index|session-id>",
}

_ALIASES = {
    "quit": "exit",
    "q": "exit",
    "switch": "agent",
    "list": "sessions",
    "ls": "sessions",
    "h": "help",
    "?": "help",
}


class SlashCommands:
    """Parse and dispatch ``/`` commands against a host REPL."""

    def __init__(self, repl: Any):
        self._repl = repl

    @staticmethod
    def is_command(line: str) -> bool:
        return line.strip().startswith("/")

    async def handle(self, line: str) -> None:
        """Dispatch one command line. Unknown commands print a hint."""
        body = line.strip()[1:].strip()
        if not body:
            return self._cmd_help("")
        parts = body.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        name = _ALIASES.get(name, name)
        if name not in _COMMANDS:
            self._repl._notice(f"Unknown command: /{name}. Type /help for the list.\n")
            return
        getattr(self, f"_cmd_{name}")(arg)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------
    def _cmd_help(self, _arg: str) -> None:
        lines = ["Commands:"]
        for name, desc in _COMMANDS.items():
            lines.append(f"  /{name:<10} {desc}")
        lines.append("")
        self._repl._notice("\n".join(lines))

    def _cmd_exit(self, _arg: str) -> None:
        self._repl.request_exit()

    def _cmd_agents(self, _arg: str) -> None:
        agents = self._repl.active_agents()
        if not agents:
            self._repl._notice("(no agents)\n")
            return
        current = self._repl.current_agent_id
        lines = ["Agents:"]
        for i, (agent_id, name, status) in enumerate(agents):
            marker = "*" if agent_id == current else " "
            lines.append(f" {marker}[{i}] {name}  {agent_id[:8]}  ({status})")
        lines.append("")
        self._repl._notice("\n".join(lines))

    def _cmd_agent(self, arg: str) -> None:
        if not arg:
            self._repl._notice("usage: /agent <index|session-id|name>\n")
            return
        result = self._repl.switch_agent(arg)
        if result is None:
            self._repl._notice(f"no agent matching '{arg}'.\n")
            return
        agent_id, name = result
        self._repl._notice(f"switched to {name} ({agent_id[:8]}).\n")

    def _cmd_new(self, arg: str) -> None:
        agent_id = self._repl.new_agent(arg or "Assistant")
        if agent_id is None:
            self._repl._notice("cannot create a new agent here.\n")
            return
        self._repl._notice(f"created agent {agent_id[:8]} (active).\n")

    def _cmd_fork(self, _arg: str) -> None:
        agent_id = self._repl.fork_current()
        if agent_id is None:
            self._repl._notice("cannot fork the current session.\n")
            return
        self._repl._notice(f"forked into agent {agent_id[:8]} (active).\n")

    def _cmd_sessions(self, _arg: str) -> None:
        sessions = self._repl.list_resumable_sessions()
        if not sessions:
            self._repl._notice("(no resumable sessions)\n")
            return
        lines = ["Sessions (newest first):"]
        for i, info in enumerate(sessions):
            label = info.title or info.last_prompt or info.preview or "(no preview)"
            label = label.replace("\n", " ")[:60]
            lines.append(f"  [{i}] {info.session_id[:8]}  {info.modified[:19]}  {label}")
        lines.append("")
        self._repl._notice("\n".join(lines))

    def _cmd_resume(self, arg: str) -> None:
        if not arg:
            self._repl._notice("usage: /resume <index|session-id>\n")
            return
        ok, message = self._repl.resume_session_ref(arg)
        self._repl._notice(message + "\n")


__all__ = ["SlashCommands"]
