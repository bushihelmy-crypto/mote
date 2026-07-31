"""Pure data exchanged between hook runners and their consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from mote.contracts.authorization import PermissionBehavior

HookEvent = Literal[
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "SessionStart",
    "Stop",
    "PreCompact",
    "PostCompact",
    "FileChanged",
]
HookBehavior = PermissionBehavior


@dataclass
class HookOutcome:
    """The folded influence a hook event has on its host."""

    behavior: Optional[HookBehavior] = None
    updated_args: Optional[dict] = None
    updated_response: Optional[str] = None
    additional_context: list[str] = field(default_factory=list)
    system_message: str = ""
    stop: bool = False
    stop_reason: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.behavior == "deny" or self.stop


__all__ = ["HookBehavior", "HookEvent", "HookOutcome"]
