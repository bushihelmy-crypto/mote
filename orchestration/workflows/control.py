"""Workflow pause handoff used by orchestration execution hosts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PauseReason(str, Enum):
    LLM_ROUTE = "llm_route"
    STALL = "stall"


@dataclass
class WorkflowPause:
    reason: PauseReason
    state: Any
    completed: set
    run_state: Any = None
    edge: Any = None
    stalled_nodes: tuple[str, ...] = ()


__all__ = ["PauseReason", "WorkflowPause"]
