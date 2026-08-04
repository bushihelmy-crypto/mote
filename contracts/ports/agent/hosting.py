"""Narrow immutable capabilities required to host one resident Agent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from mote.contracts.ports.agent.budget import AgentBudgetPort
from mote.contracts.ports.workflow.delivery import WorkflowAgentDeliveryCompositionPort
from mote.contracts.ports.workflow.governance import WorkflowGovernanceCompositionPort

ResultT = TypeVar("ResultT")


class DurableWritePort(Protocol):
    def enqueue(self, key: str, fn: Callable[[], object]) -> None: ...

    async def submit(self, key: str, fn: Callable[[], ResultT]) -> ResultT: ...

    async def drain(self) -> None: ...

    def flush_inline(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ResidentAgentHostingSnapshot:
    workspace_root: Path
    writer: DurableWritePort
    budget: AgentBudgetPort
    workflow_governance: WorkflowGovernanceCompositionPort | None
    workflow_delivery: WorkflowAgentDeliveryCompositionPort | None


__all__ = ["DurableWritePort", "ResidentAgentHostingSnapshot"]
