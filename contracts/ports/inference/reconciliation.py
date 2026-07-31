"""Narrow ports for daemon evidence and logical-owner reconciliation."""

from typing import Protocol

from mote.contracts.inference.provider_evidence import ProviderEvidence, ProviderEvidenceQuery
from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.contracts.inference.reconciliation import (
    OwnerAcknowledgement,
    OwnerCommand,
    ReconciliationState,
    ResolutionProposal,
)


class ReconciliationRecordView(Protocol):
    proposal: ResolutionProposal
    state: ReconciliationState
    acknowledgement: OwnerAcknowledgement | None


class ProviderEvidenceQueryPort(Protocol):
    async def query(self, request: ProviderEvidenceQuery) -> ProviderEvidence | None:
        ...


class ReconciliationAuthorityPort(Protocol):
    async def provider_for(self, execution_id: str, generation_id: str) -> str:
        ...

    async def append(self, evidence: ProviderEvidence) -> bool:
        ...

    async def propose(self, execution_id: str) -> ReconciliationRecordView:
        ...

    async def list_records(
        self, *, state: ReconciliationState | None = None, limit: int = 100
    ) -> tuple[ReconciliationRecordView, ...]:
        ...

    async def acknowledge(self, acknowledgement: OwnerAcknowledgement) -> ReconciliationRecordView:
        ...

    async def read_owner_commands(
        self, owner_id: str, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[tuple[int, OwnerCommand], ...]:
        ...


class InDoubtReceiptSource(Protocol):
    async def list_receipts(self, *, state: ReceiptState | None = None, limit: int = 100) -> tuple[AttemptReceipt, ...]:
        ...


class OwnerCommandSource(Protocol):
    async def read_owner_commands(
        self, owner_id: str, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[tuple[int, OwnerCommand], ...]:
        ...

    async def acknowledge(self, acknowledgement: OwnerAcknowledgement) -> ReconciliationRecordView:
        ...


class LogicalOwnerJournal(Protocol):
    async def apply_reconciliation(self, command: OwnerCommand) -> int:
        ...


__all__ = [
    "InDoubtReceiptSource",
    "LogicalOwnerJournal",
    "OwnerCommandSource",
    "ProviderEvidenceQueryPort",
    "ReconciliationAuthorityPort",
    "ReconciliationRecordView",
]
