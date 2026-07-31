"""Bounded daemon evidence scanning and logical-owner proposal consumption."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from mote.contracts.inference.provider_evidence import ProviderEvidenceQuery
from mote.contracts.inference.receipt import ReceiptState
from mote.contracts.inference.reconciliation import OwnerAcknowledgement, OwnerDecision
from mote.contracts.ports.inference.reconciliation import (
    InDoubtReceiptSource,
    LogicalOwnerJournal,
    OwnerCommandSource,
    ProviderEvidenceQueryPort,
    ReconciliationAuthorityPort,
)


class DaemonEvidenceReconciler:
    def __init__(
        self,
        *,
        receipts: InDoubtReceiptSource,
        authority: ReconciliationAuthorityPort,
        provider_query: ProviderEvidenceQueryPort,
        concurrency: int = 8,
        scan_limit: int = 100,
        query_timeout_seconds: float = 10.0,
    ) -> None:
        if concurrency <= 0 or scan_limit <= 0 or scan_limit > 1000:
            raise ValueError("reconciliation scan bounds are invalid")
        if query_timeout_seconds <= 0:
            raise ValueError("reconciliation query timeout must be positive")
        self._receipts = receipts
        self._authority = authority
        self._provider_query = provider_query
        self._concurrency = concurrency
        self._scan_limit = scan_limit
        self._query_timeout = query_timeout_seconds

    async def scan_once(self) -> tuple[int, int]:
        receipts = await self._receipts.list_receipts(state=ReceiptState.IN_DOUBT, limit=self._scan_limit)
        semaphore = asyncio.Semaphore(self._concurrency)

        async def reconcile(receipt) -> bool:
            async with semaphore:
                deadline = datetime.now(timezone.utc) + timedelta(seconds=self._query_timeout)
                query = ProviderEvidenceQuery(
                    provider=await self._authority.provider_for(receipt.attempt_id, receipt.generation_id),
                    execution_id=receipt.attempt_id,
                    generation_id=receipt.generation_id,
                    provider_resource_id=receipt.provider_request_id,
                    attempt=1,
                    deadline=deadline,
                )
                try:
                    evidence = await asyncio.wait_for(
                        self._provider_query.query(query),
                        timeout=self._query_timeout,
                    )
                except TimeoutError:
                    return False
                if evidence is None:
                    return False
                await self._authority.append(evidence)
                await self._authority.propose(receipt.attempt_id)
                return True

        outcomes = await asyncio.gather(*(reconcile(item) for item in receipts))
        return len(receipts), sum(outcomes)


class CallerLogicalReconciler:
    def __init__(
        self,
        *,
        owner_id: str,
        generation_id: str,
        strategy_id: str,
        commands: OwnerCommandSource,
        journal: LogicalOwnerJournal,
    ) -> None:
        if not owner_id or not generation_id or not strategy_id:
            raise ValueError("logical reconciliation identity is required")
        self._owner_id = owner_id
        self._generation_id = generation_id
        self._strategy_id = strategy_id
        self._commands = commands
        self._journal = journal

    async def consume(self, *, after_sequence: int = 0, limit: int = 100) -> int:
        records = await self._commands.read_owner_commands(self._owner_id, after_sequence=after_sequence, limit=limit)
        cursor = after_sequence
        for sequence, command in records:
            if command.owner_id != self._owner_id:
                raise PermissionError("owner command identity mismatch")
            if command.generation_id != self._generation_id or command.strategy_id != self._strategy_id:
                acknowledgement = OwnerAcknowledgement(
                    proposal_id=command.proposal_id,
                    owner_id=self._owner_id,
                    decision=OwnerDecision.REJECT,
                    owner_journal_revision=1,
                )
            else:
                revision = await self._journal.apply_reconciliation(command)
                acknowledgement = OwnerAcknowledgement(
                    proposal_id=command.proposal_id,
                    owner_id=self._owner_id,
                    decision=OwnerDecision.APPLY,
                    owner_journal_revision=revision,
                )
            await self._commands.acknowledge(acknowledgement)
            cursor = sequence
        return cursor


__all__ = ["CallerLogicalReconciler", "DaemonEvidenceReconciler"]
