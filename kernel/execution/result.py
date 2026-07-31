"""Internal result passed from execution to Role finalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from mote.contracts.conversation import Message

if TYPE_CHECKING:
    from mote.contracts.output import CommittedOutput

OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class ExecutionResult(Generic[OutputT]):
    presentation: Message
    committed_output: "CommittedOutput[OutputT] | None" = None


__all__ = ["ExecutionResult"]
