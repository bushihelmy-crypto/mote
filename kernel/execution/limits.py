"""Hard safety limits required by Kernel execution algorithms."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionLimits:
    graph_transitions: int = 10_000
    run_event_buffer: int = 256


DEFAULT_EXECUTION_LIMITS = ExecutionLimits()


__all__ = ["DEFAULT_EXECUTION_LIMITS", "ExecutionLimits"]
