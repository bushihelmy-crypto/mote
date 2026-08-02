"""Typed projection boundary for process-local deferred tool returns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from mote.contracts.async_work.submission import AsyncWorkSubmissionReceipt
from mote.contracts.ports.artifact.store import ReliableArtifactPublisher
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.contracts.ports.workflow.execution import WorkflowNodeExecutionPort


class DeferredResultKind(str, Enum):
    BACKGROUND_TASK = "background_task"
    WORKFLOW = "workflow"


@dataclass(frozen=True, slots=True)
class DeferredToolSettlement:
    kind: DeferredResultKind
    output: str
    execution_value: object
    submission: AsyncWorkSubmissionReceipt | None = None


class DeferredResultProjector(Protocol):
    """Product adapter that validates and projects known Orchestration values."""

    def classify(self, value: object) -> DeferredResultKind | None: ...

    def settle(
        self,
        value: object,
        *,
        tool_name: str,
    ) -> DeferredToolSettlement: ...

    async def aclose(self) -> None: ...

    def activate(self) -> None: ...

    def deactivate(self) -> None: ...


DeferredResultProjectorFactory = Callable[
    [BackgroundTaskService, ReliableArtifactPublisher, WorkflowNodeExecutionPort],
    DeferredResultProjector,
]


__all__ = [
    "DeferredResultKind",
    "DeferredResultProjector",
    "DeferredResultProjectorFactory",
    "DeferredToolSettlement",
]
