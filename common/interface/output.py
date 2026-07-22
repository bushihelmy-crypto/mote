"""Leaf protocols for output decoding and candidate evaluation."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mote.common.schema.action import FinalCandidateAction
    from mote.common.schema.output import CommittedOutput, OutputEvaluation, SchemaDocument, ValidationContext


@runtime_checkable
class OutputDecoder(Protocol):
    @property
    def schema(self) -> "SchemaDocument":
        ...

    def decode(self, raw: Any) -> Any:
        ...

    def encode(self, value: Any) -> Any:
        ...


@runtime_checkable
class OutputValidator(Protocol):
    name: str
    version: str
    stage: Any
    determinism: Any
    effect: Any

    async def validate(self, value: Any, context: "ValidationContext") -> Any:
        ...


@runtime_checkable
class OutputEngine(Protocol):
    run_id: str

    async def evaluate(self, candidate: "FinalCandidateAction") -> "OutputEvaluation":
        ...

    async def commit(self) -> "CommittedOutput":
        ...
