"""Pure data exchanged between hook runners and their consumers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Optional, cast

from mote.contracts.authorization import PermissionBehavior
from mote.contracts.events.envelope import JsonValue, freeze_json

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


@dataclass(frozen=True, slots=True)
class HookAuthorizationFact:
    """Sanitized authorization fact bound to the consuming effect identity."""

    handler_id: str
    disposition: Literal["allow", "deny"]


@dataclass(frozen=True, slots=True)
class HookStop:
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.reason) is not str:
            raise TypeError("hook stop reason must be a string")


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """The folded influence a hook event has on its host."""

    behavior: Optional[HookBehavior] = None
    updated_args: Mapping[str, JsonValue] | None = None
    updated_response: Optional[str] = None
    additional_context: tuple[str, ...] = ()
    system_message: str = ""
    stop: HookStop | None = None
    authorization_facts: tuple[HookAuthorizationFact, ...] = ()

    def __post_init__(self) -> None:
        if self.updated_args is not None:
            frozen = freeze_json(self.updated_args, path="hook updated arguments")
            if not isinstance(frozen, Mapping):
                raise TypeError("hook updated arguments must be an object")
            object.__setattr__(self, "updated_args", cast(Mapping[str, JsonValue], frozen))
        object.__setattr__(self, "additional_context", tuple(self.additional_context))
        object.__setattr__(self, "authorization_facts", tuple(self.authorization_facts))
        if any(type(item) is not str for item in self.additional_context):
            raise TypeError("hook additional context must contain strings")
        if any(not isinstance(item, HookAuthorizationFact) for item in self.authorization_facts):
            raise TypeError("hook authorization facts contain an invalid item")

    @property
    def is_blocking(self) -> bool:
        return self.behavior == "deny" or self.stop is not None


__all__ = [
    "HookAuthorizationFact",
    "HookBehavior",
    "HookEvent",
    "HookOutcome",
    "HookStop",
]
