"""Workflow-owned deferred execution descriptor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Generic, TypeVar

from mote.contracts.workflow.definition_source import WorkflowDefinitionSource

if TYPE_CHECKING:
    from mote.orchestration.workflows.definition import WorkflowExecutable
    from mote.orchestration.workflows.types import GraphRunState

DeferredResultT = TypeVar("DeferredResultT")
WorkflowPollFactory = Callable[[], Awaitable[DeferredResultT]]


class WorkflowExecutionMode(str, Enum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"
    HYBRID = "hybrid"


@dataclass
class WorkflowRunMetadata:
    request_id: str = ""
    executable: "WorkflowExecutable | None" = None
    stage_summary: str = ""
    initial_params: Mapping[str, object] | None = None
    run_state: "GraphRunState | None" = None
    from_nodes: tuple[str, ...] = ()
    skip_nodes: tuple[str, ...] = ()
    definition_source: WorkflowDefinitionSource | None = None
    # Durable checkpoint/frontier projection.  Resume authorities must use
    # these committed facts, never a live graph object.
    checkpoint: bytes | None = None
    pending_frontier: tuple[str, ...] = ()
    run_revision: int | None = None
    execution_fence: int | None = None


@dataclass
class WorkflowDeferredResult(Generic[DeferredResultT]):
    mode: WorkflowExecutionMode = WorkflowExecutionMode.FOREGROUND
    result: DeferredResultT | None = None
    poll_factory: WorkflowPollFactory[DeferredResultT] | None = field(default=None, repr=False)
    command_name: str = ""
    graph_meta: WorkflowRunMetadata | None = field(default=None, repr=False)

    @classmethod
    def background(
        cls,
        poll_factory: WorkflowPollFactory[DeferredResultT],
        *,
        command_name: str,
        graph_meta: WorkflowRunMetadata | None = None,
    ) -> "WorkflowDeferredResult[DeferredResultT]":
        return cls(
            mode=WorkflowExecutionMode.BACKGROUND,
            poll_factory=poll_factory,
            command_name=command_name,
            graph_meta=graph_meta,
        )

    @classmethod
    def hybrid(
        cls,
        result: DeferredResultT,
        poll_factory: WorkflowPollFactory[DeferredResultT],
        *,
        command_name: str,
        graph_meta: WorkflowRunMetadata | None = None,
    ) -> "WorkflowDeferredResult[DeferredResultT]":
        return cls(
            mode=WorkflowExecutionMode.HYBRID,
            result=result,
            poll_factory=poll_factory,
            command_name=command_name,
            graph_meta=graph_meta,
        )


__all__ = [
    "WorkflowDeferredResult",
    "WorkflowExecutionMode",
    "WorkflowRunMetadata",
    "WorkflowPollFactory",
]
