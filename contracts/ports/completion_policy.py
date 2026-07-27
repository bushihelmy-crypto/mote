"""Narrow completion-policy seam consumed by agent loops."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mote.contracts.completion import CompletionDecision
    from mote.contracts.model_actions import ModelTurn


@runtime_checkable
class CompletionPolicy(Protocol):
    async def evaluate(self, turn: "ModelTurn") -> "CompletionDecision":
        ...
