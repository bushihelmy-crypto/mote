"""Transient control state for one agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeAlias, TypeVar

from mote.contracts.conversation import AIMessage
from mote.contracts.model.turn import ModelTurn
from mote.contracts.output import CommittedOutput

OutputT = TypeVar("OutputT")


@dataclass(frozen=True, slots=True)
class NoModelTurn:
    pass


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    turn: ModelTurn
    candidate_index: int

    def __post_init__(self) -> None:
        if self.candidate_index < 0:
            raise ValueError("candidate_index must be non-negative")
        if self.candidate_index >= len(self.turn.final_candidates):
            raise ValueError("candidate_index does not identify a final candidate")


ExecutionTurn: TypeAlias = NoModelTurn | ModelTurn | CandidateSelection


@dataclass
class ExecutionState(Generic[OutputT]):
    response: AIMessage
    committed_output: CommittedOutput[OutputT] | None = None
    turn: ExecutionTurn = field(default_factory=NoModelTurn)
    initial_observe_complete: bool = False


__all__ = ["CandidateSelection", "ExecutionState", "ExecutionTurn", "NoModelTurn"]
