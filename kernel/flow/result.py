"""Internal result passed from the flow engine to Role finalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from mote.contracts.schema import Message

if TYPE_CHECKING:
    from mote.contracts.output import CommittedOutput

OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class FlowResult(Generic[OutputT]):
    presentation: Message
    committed_output: "CommittedOutput[OutputT] | None" = None


__all__ = ["FlowResult"]
