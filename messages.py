"""Stable model-message types for the public Agent facade."""
from __future__ import annotations

from typing import TypeAlias

from mote.contracts.conversation import AIMessage, SystemMessage, ToolMessage, UserMessage

ModelMessage: TypeAlias = UserMessage | SystemMessage | AIMessage | ToolMessage

__all__ = ["ModelMessage"]
