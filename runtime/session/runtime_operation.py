"""Rollout-backed local WAL for deterministic Runtime mutations."""
from __future__ import annotations

from mote.contracts.errors.runtimes import ManagedRuntimeStateError
from mote.contracts.runtimes import (
    RuntimeCheckpoint,
    RuntimeOperationIntent,
    RuntimeOperationReceipt,
    RuntimeOperationRecovery,
)
from mote.runtime.session.events import (
    RuntimeOperationAbortedEvent,
    RuntimeOperationCompletedEvent,
    RuntimeOperationPreparedEvent,
)
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay


class SessionRuntimeOperationJournal:
    """Durably prepare before apply, then complete or abort in rollout order."""

    def __init__(self, log: SessionLog) -> None:
        self._log = log

    async def prepare(
        self,
        intent: RuntimeOperationIntent,
    ) -> RuntimeOperationReceipt | None:
        state = replay(self._log)
        receipt = state.completed_runtime_operations.get(intent.operation_id)
        if receipt is not None:
            self._assert_same_operation(intent, receipt.fingerprint)
            return receipt
        pending = state.pending_runtime_operations.get(intent.operation_id)
        if pending is not None:
            self._assert_same_operation(intent, pending.fingerprint())
            return None
        await self._log.append(RuntimeOperationPreparedEvent(intent))
        return None

    async def complete(self, receipt: RuntimeOperationReceipt) -> None:
        await self._log.append(RuntimeOperationCompletedEvent(receipt.operation_id, receipt))

    async def abort(self, operation_id: str) -> None:
        await self._log.append(RuntimeOperationAbortedEvent(operation_id))

    async def recovery(
        self,
        *,
        kind: str,
        alias: str,
        checkpoint: RuntimeCheckpoint | None,
    ) -> RuntimeOperationRecovery:
        state = replay(self._log)
        if checkpoint is None:
            checkpoint = state.runtime_checkpoints.get(f"{kind}:{alias}")
        pending = [
            intent
            for intent in state.pending_runtime_operations.values()
            if intent.kind == kind and intent.alias == alias
        ]
        if checkpoint is not None:
            already_applied = [
                intent
                for intent in pending
                if intent.runtime_id == checkpoint.runtime_id
                and intent.epoch == checkpoint.epoch
                and intent.target_revision <= checkpoint.revision
            ]
            for intent in already_applied:
                await self.complete(RuntimeOperationReceipt.from_intent(intent))
            operations = tuple(
                sorted(
                    (
                        intent
                        for intent in pending
                        if intent.runtime_id == checkpoint.runtime_id
                        and intent.epoch == checkpoint.epoch
                        and intent.target_revision > checkpoint.revision
                    ),
                    key=lambda intent: intent.target_revision,
                )
            )
            return RuntimeOperationRecovery(
                checkpoint=checkpoint,
                operations=operations,
            )
        if not pending:
            return RuntimeOperationRecovery()
        newest = max(pending, key=lambda intent: (intent.epoch, intent.target_revision))
        operations = tuple(
            sorted(
                (
                    intent
                    for intent in pending
                    if intent.runtime_id == newest.runtime_id and intent.epoch == newest.epoch
                ),
                key=lambda intent: intent.target_revision,
            )
        )
        return RuntimeOperationRecovery(
            checkpoint=operations[0].base_checkpoint,
            operations=operations,
        )

    @staticmethod
    def _assert_same_operation(
        intent: RuntimeOperationIntent,
        fingerprint: str,
    ) -> None:
        if intent.fingerprint() != fingerprint:
            raise ManagedRuntimeStateError(
                "runtime operation_id was reused with a different mutation",
                operation_id=intent.operation_id,
            )


__all__ = ["SessionRuntimeOperationJournal"]
