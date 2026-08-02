"""Strict identity union for the two asynchronous-work domains."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, TypeAlias

from mote.contracts.task.lifecycle import LocalTaskReference
from mote.contracts.workflow.identity import WorkflowRunReference


class AsyncWorkKind(str, Enum):
    LOCAL_BACKGROUND_TASK = "local_background_task"
    DURABLE_WORKFLOW_RUN = "durable_workflow_run"


@dataclass(frozen=True, slots=True)
class LocalBackgroundTaskReference:
    reference: LocalTaskReference
    kind: Literal[AsyncWorkKind.LOCAL_BACKGROUND_TASK] = field(default=AsyncWorkKind.LOCAL_BACKGROUND_TASK, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reference, LocalTaskReference):
            raise TypeError("local async-work reference requires LocalTaskReference")


@dataclass(frozen=True, slots=True)
class DurableWorkflowRunReference:
    reference: WorkflowRunReference
    kind: Literal[AsyncWorkKind.DURABLE_WORKFLOW_RUN] = field(default=AsyncWorkKind.DURABLE_WORKFLOW_RUN, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reference, WorkflowRunReference):
            raise TypeError("durable async-work reference requires WorkflowRunReference")


AsyncWorkReference: TypeAlias = LocalBackgroundTaskReference | DurableWorkflowRunReference


__all__ = [
    "AsyncWorkKind",
    "AsyncWorkReference",
    "DurableWorkflowRunReference",
    "LocalBackgroundTaskReference",
]
