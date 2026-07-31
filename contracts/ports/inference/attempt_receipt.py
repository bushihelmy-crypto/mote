from typing import Protocol

from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState


class AttemptReceiptStore(Protocol):
    async def get(self, attempt_id: str, generation_id: str) -> AttemptReceipt | None:
        ...

    async def list_receipts(self, *, state: ReceiptState | None = None, limit: int = 100) -> tuple[AttemptReceipt, ...]:
        ...

    async def accept(self, receipt: AttemptReceipt) -> AttemptReceipt:
        ...

    async def compare_and_swap(
        self,
        receipt: AttemptReceipt,
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> AttemptReceipt:
        ...
