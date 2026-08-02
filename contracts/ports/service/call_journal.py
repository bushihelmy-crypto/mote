"""Durable storage boundary for external Tool service calls."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol

from mote.contracts.service import PendingServiceCall, ServiceCallJournalRecord, ServiceCallRecovery


class ServiceCallJournal(Protocol):
    def claim(self, service_call_id: str) -> "ServiceCallOwnershipClaim": ...

    async def append(self, record: ServiceCallJournalRecord) -> None: ...

    def records(self, service_call_id: str) -> tuple[ServiceCallJournalRecord, ...]: ...

    def recover(self, service_call_id: str) -> ServiceCallRecovery: ...

    async def pending_calls(self, *, after: str | None, limit: int) -> tuple[PendingServiceCall, ...]: ...

    async def request_cancel(self, service_call_id: str) -> None: ...

    def cancellation_requested(self, service_call_id: str) -> bool: ...


class ServiceCallOwnershipClaim(Protocol):
    async def __aenter__(self) -> "ServiceCallOwnershipClaim": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    @property
    def generation(self) -> int: ...


__all__ = ["ServiceCallJournal", "ServiceCallOwnershipClaim"]
