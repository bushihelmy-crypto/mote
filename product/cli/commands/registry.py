"""Explicit construction of the Product's builtin slash-command catalog."""
from __future__ import annotations

from mote.product.cli.commands.builtin import BUILTIN_COMMANDS
from mote.product.cli.commands.core import Command, CommandRegistry, register_command


def default_registry() -> CommandRegistry:
    """Build an isolated registry populated with immutable builtin definitions."""

    registry = CommandRegistry()
    for command in BUILTIN_COMMANDS:
        registry.register(command)
    return registry


__all__ = ["Command", "CommandRegistry", "register_command", "default_registry"]
