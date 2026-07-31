from typing import Protocol

from mote.contracts.inference.session import SessionReceipt


class SessionReceiptStore(Protocol):
    async def get(self, session_id: str, generation_id: str) -> SessionReceipt | None:
        ...

    async def accept(self, receipt: SessionReceipt) -> SessionReceipt:
        ...

    async def compare_and_swap(
        self,
        receipt: SessionReceipt,
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> SessionReceipt:
        ...
