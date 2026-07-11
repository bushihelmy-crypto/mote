#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``Command`` + ``CommandRegistry`` — slash commands as self-registering objects.

The §8 successor of ``SlashCommands``: a command is an object (name + aliases +
help + an ``async (ctx, arg)`` handler) registered into a module-level table via
``@register_command``, so a new command is a new function + a decorator with zero
dispatcher edits (§2.7, mirroring the consumer registry).

Crucially, command *output* no longer writes stdout directly (the old
``self._repl._notice``). Handlers call ``ctx.notice(text)`` on the host surface,
which emits a ``Notice`` ``ViewEvent`` → every active consumer renders it
correctly (terminal prints, Web pushes, 飞书 sends a card). The ``ctx`` is the
:class:`SessionDriver` surface, duck-typed: ``notice`` / ``request_exit`` /
``current_agent_id`` / ``active_agents`` / ``switch_agent`` / ``new_agent`` /
``fork_current`` / ``list_resumable_sessions`` / ``resume_session_ref``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

CommandHandler = Callable[[Any, str], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    """One slash command: canonical name + aliases + one-line help + handler."""

    name: str
    handler: CommandHandler
    help: str = ""
    aliases: Tuple[str, ...] = field(default_factory=tuple)


class CommandRegistry:
    """A table of :class:`Command` objects with alias resolution + dispatch."""

    def __init__(self) -> None:
        self._by_name: Dict[str, Command] = {}
        self._aliases: Dict[str, str] = {}

    def register(self, command: Command) -> Command:
        self._by_name[command.name] = command
        for alias in command.aliases:
            self._aliases[alias] = command.name
        return command

    def is_command(self, line: str) -> bool:
        """True only when *line* is a ``/`` prefix whose first token *matches* a
        registered command. A ``/``-prefixed line that resolves to nothing (a path
        like ``/home/x``, or prose) is NOT a command — it's ordinary conversation.
        """
        stripped = line.strip()
        if not stripped.startswith("/"):
            return False
        body = stripped[1:].strip()
        if not body:
            return False  # a bare "/" matches nothing → conversation
        name = body.split(maxsplit=1)[0].lower()
        return self.resolve(name) is not None

    def resolve(self, name: str) -> Optional[Command]:
        canonical = self._aliases.get(name, name)
        return self._by_name.get(canonical)

    def commands(self) -> Tuple[Command, ...]:
        # De-dup (aliases share an object) and present in registration order.
        return tuple(self._by_name.values())

    async def handle(self, ctx: Any, line: str) -> None:
        """Dispatch one command line; an unknown command notices a hint."""
        body = line.strip()[1:].strip()
        if not body:
            return await self._dispatch_help(ctx)
        parts = body.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        command = self.resolve(name)
        if command is None:
            ctx.notice(f"Unknown command: /{name}. Type /help for the list.\n")
            return
        await command.handler(ctx, arg)

    async def _dispatch_help(self, ctx: Any) -> None:
        help_cmd = self.resolve("help")
        if help_cmd is not None:
            await help_cmd.handler(ctx, "")

    def help_text(self) -> str:
        lines = ["Commands:"]
        for command in self.commands():
            lines.append(f"  /{command.name:<10} {command.help}")
        lines.append("")
        return "\n".join(lines)


# Module-level default registry + decorator (self-registration, §2.7).
_DEFAULT_REGISTRY = CommandRegistry()


def register_command(
    name: str, *, help: str = "", aliases: Tuple[str, ...] = ()
) -> Callable[[CommandHandler], CommandHandler]:
    """Register a handler as a :class:`Command` in the default registry."""

    def deco(handler: CommandHandler) -> CommandHandler:
        _DEFAULT_REGISTRY.register(Command(name=name, handler=handler, help=help, aliases=aliases))
        return handler

    return deco


def default_registry() -> CommandRegistry:
    """Return the default registry with builtin commands imported once."""
    _ensure_builtins_imported()
    return _DEFAULT_REGISTRY


_builtins_imported = False


def _ensure_builtins_imported() -> None:
    global _builtins_imported
    if _builtins_imported:
        return
    _builtins_imported = True
    try:
        import mote.cli.commands.builtin  # noqa: F401 — registers on import
    except Exception:  # noqa: BLE001 — a broken builtin must not kill dispatch
        pass


__all__ = [
    "Command",
    "CommandRegistry",
    "register_command",
    "default_registry",
]
