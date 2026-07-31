"""Protocol-neutral immutable tool semantics."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    semantic_identity: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.semantic_identity.strip():
            raise ValueError("tool name and semantic identity are required")

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def renamed(self, name: str) -> "ToolDefinition":
        return replace(self, name=name)


__all__ = ["ToolDefinition"]
