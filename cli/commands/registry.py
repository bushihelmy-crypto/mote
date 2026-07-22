"""Public slash-command registry and builtin discovery boundary."""
from __future__ import annotations

from mote.cli.commands.core import Command, CommandRegistry, register_command, registered_commands

_builtins_imported = False


def default_registry() -> CommandRegistry:
    """Return the default registry after loading builtin commands once."""
    _ensure_builtins_imported()
    return registered_commands()


def _ensure_builtins_imported() -> None:
    global _builtins_imported
    if _builtins_imported:
        return
    _builtins_imported = True
    try:
        import mote.cli.commands.builtin  # noqa: F401 — registers on import
    except Exception:  # noqa: BLE001 — a broken builtin must not kill dispatch
        pass


__all__ = ["Command", "CommandRegistry", "register_command", "default_registry"]
