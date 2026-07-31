"""Product adapter between workflow runs and background operations."""

from __future__ import annotations

import asyncio

from mote.orchestration.background_tasks.operation import (
    OperationCancelled,
    OperationFailed,
    OperationOutcome,
    OperationPaused,
    OperationSucceeded,
    OperationTimedOut,
    ResumeRef,
    StopDisposition,
    StopReason,
)
from mote.orchestration.workflows import (
    Cancelled,
    Failed,
    Paused,
    Succeeded,
    TimedOut,
    WorkflowContinuation,
    WorkflowOutcome,
    WorkflowRun,
)
from mote.product.workflows.continuation_registry import WorkflowContinuationRegistry


class WorkflowTaskAdapter:
    """Expose one WorkflowRun through the DeferredOperation contract."""

    def __init__(
        self,
        run: WorkflowRun,
        continuations: WorkflowContinuationRegistry,
    ) -> None:
        self._run = run
        self._continuations = continuations
        self._task: asyncio.Task[WorkflowOutcome] | None = None
        self._closed = False
        self._terminal: OperationOutcome | None = None
        self._terminal_lock = asyncio.Lock()

    def _resume_ref(self, continuation: WorkflowContinuation | None) -> ResumeRef | None:
        if continuation is None:
            return None
        return ResumeRef(self._continuations.register(continuation))

    def _translate(self, outcome: WorkflowOutcome) -> OperationOutcome:
        if isinstance(outcome, Succeeded):
            return OperationSucceeded(outcome.output)
        if isinstance(outcome, Failed):
            return OperationFailed(outcome.error, self._resume_ref(outcome.continuation))
        if isinstance(outcome, Paused):
            return OperationPaused(outcome.reason, self._resume_ref(outcome.continuation))
        if isinstance(outcome, TimedOut):
            return OperationTimedOut(outcome.reason, self._resume_ref(outcome.continuation))
        assert isinstance(outcome, Cancelled)
        return OperationCancelled(outcome.reason, self._resume_ref(outcome.continuation))

    async def execute(self) -> OperationOutcome:
        async with self._terminal_lock:
            if self._terminal is not None:
                return self._terminal
        if self._closed or self._task is not None:
            raise RuntimeError("WorkflowTaskAdapter instances are single-use")
        self._task = asyncio.create_task(self._run.execute())
        translated = self._translate(await self._task)
        async with self._terminal_lock:
            if self._terminal is None:
                self._terminal = translated
            return self._terminal

    async def request_stop(self, reason: StopReason, disposition: StopDisposition) -> OperationOutcome:
        continuation = self._run.continuation() if disposition is StopDisposition.CHECKPOINT else None
        ref = self._resume_ref(continuation)
        if reason is StopReason.TIMEOUT:
            requested: OperationOutcome = OperationTimedOut(resume_ref=ref)
        else:
            requested = OperationCancelled(reason.value, ref)
        async with self._terminal_lock:
            if self._terminal is None:
                self._terminal = requested
            terminal = self._terminal
        await self._run.aclose()
        return terminal

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._run.aclose()


__all__ = ["WorkflowTaskAdapter"]
