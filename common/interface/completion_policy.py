"""Narrow completion-policy seam consumed by agent loops."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mote.common.schema.action import ModelTurn
    from mote.common.schema.completion import CompletionDecision


@runtime_checkable
class CompletionPolicy(Protocol):
    async def evaluate(self, turn: "ModelTurn") -> "CompletionDecision":
        ...
