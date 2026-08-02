"""Workflow-owned deferred execution descriptor."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from mote.contracts.workflow.definition_source import WorkflowDefinitionSource


class WorkflowExecutionMode(str, Enum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"
    HYBRID = "hybrid"


@dataclass
class WorkflowRunMetadata:
    request_id: str = ""
    graph_ref: Any = None
    initial_params: dict | None = None
    run_state: Any = None
    state: Any = None
    from_nodes: tuple[str, ...] = ()
    skip_nodes: tuple[str, ...] = ()
    definition_source: WorkflowDefinitionSource | None = None


@dataclass
class WorkflowDeferredResult:
    mode: WorkflowExecutionMode = WorkflowExecutionMode.FOREGROUND
    result: Any = None
    poll_factory: Callable[[], Coroutine] | None = field(default=None, repr=False)
    command_name: str = ""
    graph_meta: WorkflowRunMetadata | None = field(default=None, repr=False)

    @classmethod
    def background(
        cls,
        poll_factory: Callable[[], Coroutine],
        *,
        command_name: str,
        graph_meta: WorkflowRunMetadata | None = None,
    ) -> "WorkflowDeferredResult":
        return cls(
            mode=WorkflowExecutionMode.BACKGROUND,
            poll_factory=poll_factory,
            command_name=command_name,
            graph_meta=graph_meta,
        )

    @classmethod
    def hybrid(
        cls,
        result: Any,
        poll_factory: Callable[[], Coroutine],
        *,
        command_name: str,
        graph_meta: WorkflowRunMetadata | None = None,
    ) -> "WorkflowDeferredResult":
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
]
