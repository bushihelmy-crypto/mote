"""Consumer-owned completion policy for one model turn."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.model.turn import ModelTurn
from mote.contracts.output.completion import CompletionDecision


class ModelTurnCompletionPolicy(Protocol):
    """Classify the semantic actions produced by one inference turn."""

    async def evaluate(self, turn: ModelTurn) -> CompletionDecision: ...


__all__ = ["ModelTurnCompletionPolicy"]
