"""Consumer-owned ports for process-local async work."""

from typing import Protocol

from mote.contracts.async_work.command import CancelLocalBackgroundTask, LocalCancelReceipt
from mote.contracts.async_work.identity import LocalBackgroundTaskReference
from mote.contracts.ports.async_work.observation import AsyncWorkQueryResult


class LocalAsyncWorkObservationPort(Protocol):
    def get(self, reference: LocalBackgroundTaskReference) -> AsyncWorkQueryResult: ...


class LocalAsyncWorkCommandPort(Protocol):
    def cancel(self, command: CancelLocalBackgroundTask) -> LocalCancelReceipt: ...


__all__ = ["LocalAsyncWorkCommandPort", "LocalAsyncWorkObservationPort"]
