"""Read-only inference inspection without wire, permit, or credential access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState


class _ReceiptProjectionSource(Protocol):
    async def get(self, attempt_id: str, generation_id: str) -> AttemptReceipt | None:
        ...

    async def list_receipts(self, *, state: ReceiptState | None = None, limit: int = 100) -> tuple[AttemptReceipt, ...]:
        ...


@dataclass(frozen=True, slots=True)
class ReceiptInspection:
    cursor: str
    execution_id: str
    generation_id: str
    revision: int
    state: str
    operation: str
    provider_request_present: bool
    terminal_artifact_present: bool
    updated_at: datetime


class TrafficInspectorProjection:
    def __init__(self, receipts: _ReceiptProjectionSource) -> None:
        self._receipts = receipts

    async def get(self, execution_id: str, generation_id: str) -> ReceiptInspection | None:
        receipt = await self._receipts.get(execution_id, generation_id)
        return self._redact(receipt) if receipt is not None else None

    async def list(self, *, state: ReceiptState | None = None, limit: int = 100) -> tuple[ReceiptInspection, ...]:
        receipts = await self._receipts.list_receipts(state=state, limit=limit)
        return tuple(self._redact(receipt) for receipt in receipts)

    @staticmethod
    def _redact(receipt: AttemptReceipt) -> ReceiptInspection:
        return ReceiptInspection(
            cursor=f"{receipt.updated_at.isoformat()}:{receipt.attempt_id}:{receipt.generation_id}:{receipt.revision}",
            execution_id=receipt.attempt_id,
            generation_id=receipt.generation_id,
            revision=receipt.revision,
            state=receipt.state.value,
            operation=receipt.operation,
            provider_request_present=receipt.provider_request_id is not None,
            terminal_artifact_present=receipt.terminal_artifact_reference is not None,
            updated_at=receipt.updated_at,
        )
