"""Run-scoped implementation of the Kernel execution transaction contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from mote.contracts.conversation import Message
from mote.contracts.events.conversation import MessageAppendedEvent
from mote.contracts.execution.models import (
    ExecutionOperationContext,
    ExecutionRecoveryFrontier,
    MutationResult,
    MutationStatus,
)
from mote.contracts.output import CommittedOutput, ValidatedCandidate
from mote.contracts.ports.conversation.message_store import MessageStore
from mote.contracts.ports.execution.checkpoint import InferenceCheckpointPort
from mote.contracts.ports.execution.transaction import HistoryProjection
from mote.contracts.ports.output.evaluation import OutputEngine
from mote.contracts.ports.session.facts import SessionFactSink

OutputT = TypeVar("OutputT")


class RuntimeExecutionTransaction(Generic[OutputT]):
    def __init__(
        self,
        *,
        run_id: str,
        fencing_token: int,
        memory: MessageStore,
        output_engine: OutputEngine[OutputT],
        inference_checkpoint: InferenceCheckpointPort,
        session_fact_sink: SessionFactSink,
        drain_writes: Callable[[], Awaitable[None]],
    ) -> None:
        self.run_id = run_id
        self.fencing_token = fencing_token
        self._memory = memory
        self._output_engine = output_engine
        self._checkpoint = inference_checkpoint
        self._session_fact_sink = session_fact_sink
        self._drain_writes = drain_writes
        self._revision = 0
        self._operations: dict[str, MutationResult | CommittedOutput[OutputT]] = {}
        self._terminal: CommittedOutput[OutputT] | None = None
        self._cancelled = False

    def context(self, operation_id: str, *, attempt_id: str = "") -> ExecutionOperationContext:
        return ExecutionOperationContext(
            run_id=self.run_id,
            attempt_id=attempt_id,
            operation_id=operation_id,
            fencing_token=self.fencing_token,
        )

    async def record_history(self, context: ExecutionOperationContext, history: HistoryProjection) -> MutationResult:
        return await self._record_projection(context, history)

    async def record_effect_intent(
        self, context: ExecutionOperationContext, projection: HistoryProjection
    ) -> MutationResult:
        return await self._record_projection_with_checkpoint(context, projection)

    async def settle_effect_batch(
        self, context: ExecutionOperationContext, projection: HistoryProjection
    ) -> MutationResult:
        return await self._record_projection(context, projection)

    async def record_local_action_batch(
        self, context: ExecutionOperationContext, projection: HistoryProjection
    ) -> MutationResult:
        return await self._record_projection_with_checkpoint(context, projection)

    async def reject_output(self, context: ExecutionOperationContext, rejection: HistoryProjection) -> MutationResult:
        return await self._record_projection_with_checkpoint(context, rejection)

    async def commit_final_output(
        self,
        context: ExecutionOperationContext,
        output: ValidatedCandidate[OutputT],
        message: Message,
    ) -> CommittedOutput[OutputT] | MutationResult:
        prior = self._operations.get(context.operation_id)
        if prior is not None:
            return prior
        invalid = self._validate(context)
        if invalid is not None:
            return invalid
        if self._terminal is not None:
            if self._terminal.candidate_id == output.candidate_id:
                return self._terminal
            return self._store_conflict(context, "a different final output is already committed")
        if self._output_engine.validated_candidate != output:
            return self._store_conflict(context, "validated output does not match output engine")
        consumption = await self._checkpoint.prepare_consumption(context.operation_id)
        committed = await self._output_engine.commit_final(
            message,
            companion_facts=(consumption,),
            fact_sink=self._session_fact_sink,
        )
        self._memory.apply_committed_messages((message,))
        self._checkpoint.acknowledge_consumption(consumption)
        self._revision += 1
        self._terminal = committed
        self._operations[context.operation_id] = committed
        return committed

    async def recover_frontier(self, run_id: str) -> ExecutionRecoveryFrontier:
        if run_id != self.run_id:
            return ExecutionRecoveryFrontier(revision=0, cancelled=True)
        return ExecutionRecoveryFrontier(
            revision=self._revision,
            terminal_committed=self._terminal is not None,
            cancelled=self._cancelled,
        )

    async def _record_projection(
        self,
        context: ExecutionOperationContext,
        projection: HistoryProjection,
    ) -> MutationResult:
        prior = self._prior(context)
        if prior is not None:
            return prior
        invalid = self._validate(context)
        if invalid is not None:
            return invalid
        await self._memory.add_batch(list(projection.messages))
        return await self._apply(context, reference_id=projection.fingerprint)

    async def _record_projection_with_checkpoint(
        self,
        context: ExecutionOperationContext,
        projection: HistoryProjection,
    ) -> MutationResult:
        prior = self._prior(context)
        if prior is not None:
            return prior
        invalid = self._validate(context)
        if invalid is not None:
            return invalid
        consumption = await self._checkpoint.prepare_consumption(context.operation_id)
        events = tuple(MessageAppendedEvent(message=message) for message in projection.messages) + (consumption,)
        await self._session_fact_sink.commit_facts(events)
        self._memory.apply_committed_messages(projection.messages)
        self._checkpoint.acknowledge_consumption(consumption)
        return self._mark_applied(context, reference_id=projection.fingerprint)

    async def _apply(self, context: ExecutionOperationContext, *, reference_id: str) -> MutationResult:
        await self._drain_writes()
        return self._mark_applied(context, reference_id=reference_id)

    def _mark_applied(self, context: ExecutionOperationContext, *, reference_id: str) -> MutationResult:
        self._revision += 1
        result = MutationResult(
            MutationStatus.APPLIED,
            revision=self._revision,
            reference_id=reference_id,
        )
        self._operations[context.operation_id] = result
        return result

    def _prior(self, context: ExecutionOperationContext) -> MutationResult | None:
        prior = self._operations.get(context.operation_id)
        return (
            MutationResult(
                MutationStatus.ALREADY_APPLIED,
                revision=getattr(prior, "revision", self._revision),
                reference_id=getattr(prior, "reference_id", ""),
            )
            if prior is not None
            else None
        )

    def _validate(self, context: ExecutionOperationContext) -> MutationResult | None:
        if self._cancelled:
            return MutationResult(MutationStatus.CANCELLED, revision=self._revision)
        if context.run_id != self.run_id or context.fencing_token != self.fencing_token:
            return MutationResult(MutationStatus.FENCED, revision=self._revision)
        if context.expected_revision is not None and context.expected_revision != self._revision:
            return MutationResult(
                MutationStatus.CONFLICT,
                revision=self._revision,
                reason="revision mismatch",
            )
        return None

    def _store_conflict(self, context: ExecutionOperationContext, reason: str) -> MutationResult:
        result = MutationResult(
            MutationStatus.CONFLICT,
            revision=self._revision,
            reason=reason,
        )
        self._operations[context.operation_id] = result
        return result


__all__ = ["RuntimeExecutionTransaction"]
