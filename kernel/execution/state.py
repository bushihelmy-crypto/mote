"""Transient control state for one agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeAlias, TypeVar

from mote.contracts.conversation import Message
from mote.contracts.model.turn import ModelTurn
from mote.contracts.output import CommittedOutput
from mote.contracts.tool.actions import FinalCandidateAction
from mote.kernel.execution.result import ExecutionResult

OutputT = TypeVar("OutputT")


@dataclass(frozen=True, slots=True)
class NoModelTurn:
    pass


@dataclass(frozen=True, slots=True)
class PendingCandidate:
    turn: ModelTurn
    candidate_index: int

    def __post_init__(self) -> None:
        if self.candidate_index < 0:
            raise ValueError("candidate_index must be non-negative")
        if self.candidate_index >= len(self.turn.final_candidates):
            raise ValueError("candidate_index does not identify a final candidate")

    @property
    def candidate(self) -> FinalCandidateAction:
        return self.turn.final_candidates[self.candidate_index]


ExecutionTurn: TypeAlias = NoModelTurn | ModelTurn | PendingCandidate


@dataclass
class ExecutionState(Generic[OutputT]):
    response: Message
    committed_output: CommittedOutput[OutputT] | None = None
    turn: ExecutionTurn = field(default_factory=NoModelTurn)
    initial_observe_complete: bool = False
    requested_end: "ExecutionResult[OutputT] | None" = None
    continue_inference: bool = False


__all__ = ["ExecutionState", "ExecutionTurn", "NoModelTurn", "PendingCandidate"]
