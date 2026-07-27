"""Stable public events for observing one agent run."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias

from mote.kernel.flow.result import FlowResult


class RunPhase(str, Enum):
    RECOVERY = "recovery"
    OBSERVATION = "observation"
    BUDGET = "budget"
    MODEL = "model"
    INTERPRETATION = "interpretation"
    ACTION = "action"
    OUTPUT = "output"
    WAIT = "wait"


@dataclass(frozen=True)
class RunStarted:
    run_id: str


@dataclass(frozen=True)
class RunPhaseStarted:
    run_id: str
    phase: RunPhase


@dataclass(frozen=True)
class RunPhaseCompleted:
    run_id: str
    phase: RunPhase


@dataclass(frozen=True)
class RunSucceeded:
    run_id: str
    result: FlowResult[Any] | None


@dataclass(frozen=True)
class RunFailed:
    run_id: str
    error_type: str
    message: str


@dataclass(frozen=True)
class RunCancelled:
    run_id: str


RunEvent: TypeAlias = RunStarted | RunPhaseStarted | RunPhaseCompleted | RunSucceeded | RunFailed | RunCancelled


__all__ = [
    "RunCancelled",
    "RunEvent",
    "RunFailed",
    "RunPhase",
    "RunPhaseCompleted",
    "RunPhaseStarted",
    "RunStarted",
    "RunSucceeded",
]
