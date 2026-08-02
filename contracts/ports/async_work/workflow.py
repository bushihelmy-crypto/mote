"""Consumer-owned ports for durable Workflow observation and control."""

from typing import Protocol

from mote.contracts.async_work.command import (
    CancelDurableWorkflowRun,
    ResumeDurableWorkflowRun,
    WorkflowCancelReceipt,
    WorkflowResumeReceipt,
)
from mote.contracts.async_work.identity import DurableWorkflowRunReference
from mote.contracts.ports.async_work.observation import AsyncWorkQueryResult


class WorkflowAsyncWorkObservationPort(Protocol):
    def get(self, reference: DurableWorkflowRunReference) -> AsyncWorkQueryResult: ...


class WorkflowAsyncWorkCommandPort(Protocol):
    def cancel(self, command: CancelDurableWorkflowRun) -> WorkflowCancelReceipt: ...
    def resume(self, command: ResumeDurableWorkflowRun) -> WorkflowResumeReceipt: ...


__all__ = ["WorkflowAsyncWorkCommandPort", "WorkflowAsyncWorkObservationPort"]
