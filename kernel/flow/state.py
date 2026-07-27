"""Transient control state for one agent flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from mote.contracts.output import CommittedOutput
from mote.contracts.schema import AIMessage

OutputT = TypeVar("OutputT")


@dataclass
class FlowState(Generic[OutputT]):
    response: AIMessage
    committed_output: CommittedOutput[OutputT] | None = None
    turn: Any = None
    initial_observe_complete: bool = False


__all__ = ["FlowState"]
