"""Leaf protocols for output decoding and candidate evaluation."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from mote.contracts.model_actions import FinalCandidateAction
    from mote.contracts.output import (
        CommittedOutput,
        Determinism,
        OutputEvaluation,
        SchemaDocument,
        ValidationContext,
        ValidationStage,
        ValidatorDecision,
        ValidatorEffect,
    )

OutputT = TypeVar("OutputT")


@runtime_checkable
class OutputDecoder(Protocol[OutputT]):
    @property
    def schema(self) -> "SchemaDocument":
        ...

    def decode(self, raw: Any) -> OutputT:
        ...

    def encode(self, value: OutputT) -> Any:
        ...


@runtime_checkable
class OutputValidator(Protocol[OutputT]):
    name: str
    version: str
    stage: "ValidationStage"
    determinism: "Determinism"
    effect: "ValidatorEffect"

    async def validate(
        self,
        value: OutputT,
        context: "ValidationContext",
    ) -> "ValidatorDecision[OutputT]":
        ...


@runtime_checkable
class OutputEngine(Protocol[OutputT]):
    run_id: str

    async def evaluate(self, candidate: "FinalCandidateAction") -> "OutputEvaluation[OutputT]":
        ...

    async def commit(self) -> "CommittedOutput[OutputT]":
        ...
