"""Executable reference SLOs for the local durable flow runtime."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeSLO:
    recovery_records: int = 10_000
    recovery_seconds: float = 2.0
    graph_transitions: int = 10_000
    graph_seconds: float = 1.0
    disk_barrier_records: int = 1_000
    disk_barrier_seconds: float = 5.0
    shutdown_seconds: float = 5.0
    run_event_buffer: int = 256


DEFAULT_RUNTIME_SLO = RuntimeSLO()


__all__ = ["DEFAULT_RUNTIME_SLO", "RuntimeSLO"]
