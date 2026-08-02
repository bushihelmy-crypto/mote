"""Current-Agent async-work dispatch without a parallel state owner."""

from __future__ import annotations

import json
from typing import assert_never, overload

from mote.contracts.async_work.codec import encode_async_work_observation
from mote.contracts.async_work.command import (
    CancelAsyncWork,
    CancelDurableWorkflowRun,
    CancelLocalBackgroundTask,
    LocalCancelReceipt,
    WorkflowCancelReceipt,
)
from mote.contracts.async_work.identity import (
    AsyncWorkReference,
    DurableWorkflowRunReference,
    LocalBackgroundTaskReference,
)
from mote.contracts.ports.async_work import (
    LocalAsyncWorkCommandPort,
    LocalAsyncWorkObservationPort,
    WorkflowAsyncWorkCommandPort,
    WorkflowAsyncWorkObservationPort,
)
from mote.contracts.ports.async_work.observation import AsyncWorkQueryDisposition, AsyncWorkQueryResult
from mote.product.presentation.events import AsyncWorkObserved
from mote.runtime.events.context import observe_event_sync


class CurrentAgentAsyncWorkObservationService:
    """A stateless tagged-union dispatcher bound by Product composition."""

    def __init__(
        self,
        local: LocalAsyncWorkObservationPort,
        workflow: WorkflowAsyncWorkObservationPort,
    ) -> None:
        self._local = local
        self._workflow = workflow

    def get(self, reference: AsyncWorkReference) -> AsyncWorkQueryResult:
        if isinstance(reference, LocalBackgroundTaskReference):
            result = self._local.get(reference)
        elif isinstance(reference, DurableWorkflowRunReference):
            result = self._workflow.get(reference)
        else:
            assert_never(reference)
        if result.disposition is AsyncWorkQueryDisposition.FOUND and result.observation is not None:
            observe_event_sync(
                AsyncWorkObserved(
                    observation_json=json.dumps(
                        encode_async_work_observation(result.observation),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            )
        return result


class CurrentAgentAsyncWorkCommandService:
    """Dispatches commands without interpreting or weakening domain receipts."""

    def __init__(
        self,
        local: LocalAsyncWorkCommandPort,
        workflow: WorkflowAsyncWorkCommandPort,
    ) -> None:
        self._local = local
        self._workflow = workflow

    @overload
    def cancel(self, command: CancelLocalBackgroundTask) -> LocalCancelReceipt: ...

    @overload
    def cancel(self, command: CancelDurableWorkflowRun) -> WorkflowCancelReceipt: ...

    def cancel(self, command: CancelAsyncWork) -> LocalCancelReceipt | WorkflowCancelReceipt:
        if isinstance(command, CancelLocalBackgroundTask):
            return self._local.cancel(command)
        if isinstance(command, CancelDurableWorkflowRun):
            return self._workflow.cancel(command)
        assert_never(command)


__all__ = [
    "CurrentAgentAsyncWorkCommandService",
    "CurrentAgentAsyncWorkObservationService",
]
