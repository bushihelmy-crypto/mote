"""Durable storage boundary for external Tool service calls."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.service import ServiceCallJournalRecord, ServiceCallRecovery


class ServiceCallJournal(Protocol):
    async def append(self, record: ServiceCallJournalRecord) -> None:
        ...

    def records(self, service_call_id: str) -> tuple[ServiceCallJournalRecord, ...]:
        ...

    def recover(self, service_call_id: str) -> ServiceCallRecovery:
        ...


__all__ = ["ServiceCallJournal"]
