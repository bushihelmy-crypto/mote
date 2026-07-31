"""Bounded, redacted receipt lookup for long-running execution owners."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.contracts.ports.inference.attempt_receipt import AttemptReceiptStore


@dataclass(frozen=True, slots=True)
class ExecutionReceiptProjection:
    execution_id: str
    generation_id: str
    revision: int
    state: str
    operation: str
    provider_request_present: bool
    terminal_artifact_present: bool
    updated_at: datetime


class ReceiptProjection:
    def __init__(self, receipts: AttemptReceiptStore) -> None:
        self._receipts = receipts

    async def get(self, execution_id: str, generation_id: str) -> ExecutionReceiptProjection | None:
        if not execution_id or not generation_id:
            raise ValueError("receipt identity is required")
        receipt = await self._receipts.get(execution_id, generation_id)
        return self._redact(receipt) if receipt is not None else None

    async def reconciliation(self, *, limit: int = 100) -> tuple[ExecutionReceiptProjection, ...]:
        receipts = await self._receipts.list_receipts(state=ReceiptState.IN_DOUBT, limit=limit)
        return tuple(self._redact(receipt) for receipt in receipts)

    @staticmethod
    def _redact(receipt: AttemptReceipt) -> ExecutionReceiptProjection:
        return ExecutionReceiptProjection(
            execution_id=receipt.attempt_id,
            generation_id=receipt.generation_id,
            revision=receipt.revision,
            state=receipt.state.value,
            operation=receipt.operation,
            provider_request_present=receipt.provider_request_id is not None,
            terminal_artifact_present=receipt.terminal_artifact_reference is not None,
            updated_at=receipt.updated_at,
        )


__all__ = ["ExecutionReceiptProjection", "ReceiptProjection"]
