"""Local write-ahead journal for deterministic Runtime operations."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.runtimes import (
    RuntimeCheckpoint,
    RuntimeOperationIntent,
    RuntimeOperationReceipt,
    RuntimeOperationRecovery,
)


@runtime_checkable
class RuntimeOperationJournal(Protocol):
    async def prepare(
        self,
        intent: RuntimeOperationIntent,
    ) -> RuntimeOperationReceipt | None:
        ...

    async def complete(self, receipt: RuntimeOperationReceipt) -> None:
        ...

    async def abort(self, operation_id: str) -> None:
        ...

    async def recovery(
        self,
        *,
        kind: str,
        alias: str,
        checkpoint: RuntimeCheckpoint | None,
    ) -> RuntimeOperationRecovery:
        ...


__all__ = ["RuntimeOperationJournal"]
