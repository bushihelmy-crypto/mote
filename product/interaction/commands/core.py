"""Registration primitives for slash commands, with no discovery side effects."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, cast

CommandHandler = Callable[[Any, str], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    """One slash command: canonical name, aliases, help, and handler."""

    name: str
    handler: CommandHandler
    help: str = ""
    aliases: Tuple[str, ...] = field(default_factory=tuple)


class CommandRegistry:
    """A command table with alias resolution and dispatch."""

    def __init__(self) -> None:
        self._by_name: Dict[str, Command] = {}
        self._aliases: Dict[str, str] = {}

    def register(self, command: Command) -> Command:
        claimed = {command.name, *command.aliases}
        conflicts = sorted(name for name in claimed if self.resolve(name) is not None)
        if conflicts:
            raise ValueError(f"Command names already registered: {', '.join(conflicts)}")
        self._by_name[command.name] = command
        for alias in command.aliases:
            self._aliases[alias] = command.name
        return command

    def is_command(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped.startswith("/"):
            return False
        body = stripped[1:].strip()
        if not body:
            return False
        name = body.split(maxsplit=1)[0].lower()
        return self.resolve(name) is not None

    def resolve(self, name: str) -> Optional[Command]:
        canonical = self._aliases.get(name, name)
        return self._by_name.get(canonical)

    def commands(self) -> Tuple[Command, ...]:
        return tuple(self._by_name.values())

    async def handle(self, ctx: Any, line: str) -> None:
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
        help_command = self.resolve("help")
        if help_command is not None:
            await help_command.handler(ctx, "")

    def help_text(self) -> str:
        lines = ["Commands:"]
        for command in self.commands():
            lines.append(f"  /{command.name:<10} {command.help}")
        lines.append("")
        return "\n".join(lines)


_COMMAND_DEFINITION_ATTR = "__mote_command_definition__"


def register_command(
    name: str,
    *,
    help: str = "",
    aliases: Tuple[str, ...] = (),
) -> Callable[[CommandHandler], CommandHandler]:
    """Declare command metadata without mutating a process-global catalog."""

    def decorate(handler: CommandHandler) -> CommandHandler:
        setattr(
            handler,
            _COMMAND_DEFINITION_ATTR,
            Command(name=name, handler=handler, help=help, aliases=aliases),
        )
        return handler

    return decorate


def command_definition(handler: CommandHandler) -> Command:
    """Return the immutable command declared on one builtin handler."""

    definition = getattr(handler, _COMMAND_DEFINITION_ATTR, None)
    if not isinstance(definition, Command):
        raise TypeError(f"{handler!r} is not decorated with @register_command")
    return cast(Command, definition)


__all__ = ["Command", "CommandRegistry", "command_definition", "register_command"]
