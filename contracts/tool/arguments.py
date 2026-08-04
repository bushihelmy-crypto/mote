"""Canonical immutable arguments for one Tool invocation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeAlias, cast

from mote.contracts.events.envelope import JsonValue, freeze_json

ToolArguments: TypeAlias = Mapping[str, JsonValue]


def freeze_tool_arguments(value: object) -> ToolArguments:
    frozen = freeze_json(value, path="tool arguments")
    if not isinstance(frozen, Mapping):
        raise TypeError("tool arguments must be a JSON object")
    return cast(ToolArguments, frozen)


__all__ = ["ToolArguments", "freeze_tool_arguments"]
