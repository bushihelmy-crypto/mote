"""Workflow pause handoff used by orchestration execution hosts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mote.orchestration.workflows.types import GraphRunState, GraphState, _LlmEdge


class PauseReason(str, Enum):
    LLM_ROUTE = "llm_route"
    STALL = "stall"


@dataclass
class WorkflowPause:
    reason: PauseReason
    state: "GraphState"
    completed: frozenset[str]
    run_state: "GraphRunState"
    edge: "_LlmEdge | None" = None
    stalled_nodes: tuple[str, ...] = ()


__all__ = ["PauseReason", "WorkflowPause"]
