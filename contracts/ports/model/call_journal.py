"""Durable model-call journal boundary."""

from __future__ import annotations

from typing import Protocol, Sequence

from mote.contracts.model.model_journal import ModelCallJournalRecord, ModelCallRecovery


class ModelCallJournal(Protocol):
    async def append(self, record: ModelCallJournalRecord) -> None:
        ...

    def records(self, model_call_id: str) -> Sequence[ModelCallJournalRecord]:
        ...

    def recover(self, model_call_id: str) -> ModelCallRecovery:
        ...

    def in_doubt(self) -> Sequence[ModelCallRecovery]:
        ...


__all__ = ["ModelCallJournal"]
