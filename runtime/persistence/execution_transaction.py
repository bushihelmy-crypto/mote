"""Run-scoped implementation of the Kernel execution transaction contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from mote.contracts.execution.models import (
    ExecutionOperationContext,
    ExecutionRecoveryFrontier,
    MutationResult,
    MutationStatus,
)
from mote.contracts.output import AcceptedOutput, CommittedOutput
from mote.contracts.ports.conversation.message_store import MessageStore
from mote.contracts.ports.execution.checkpoint import InferenceCheckpointPort
from mote.contracts.ports.execution.transaction import HistoryProjection
from mote.contracts.ports.output.evaluation import OutputEngine

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
        drain_writes: Callable[[], Awaitable[None]],
    ) -> None:
        self.run_id = run_id
        self.fencing_token = fencing_token
        self._memory = memory
        self._output_engine = output_engine
        self._checkpoint = inference_checkpoint
        self._drain_writes = drain_writes
        self._revision = 0
        self._operations: dict[str, MutationResult | CommittedOutput[OutputT]] = {}
        self._staged = output_engine.staged_output
        self._terminal: CommittedOutput[OutputT] | None = None
        self._cancelled = False

    def context(self, operation_id: str, *, attempt_id: str = "") -> ExecutionOperationContext:
        return ExecutionOperationContext(
            run_id=self.run_id,
            attempt_id=attempt_id,
            operation_id=operation_id,
            fencing_token=self.fencing_token,
        )

    async def record_model_turn(self, context: ExecutionOperationContext, turn: HistoryProjection) -> MutationResult:
        return await self._record_projection(context, turn, consume_inference=True)

    async def record_history(self, context: ExecutionOperationContext, history: HistoryProjection) -> MutationResult:
        return await self._record_projection(context, history)

    async def record_tool_results(
        self,
        context: ExecutionOperationContext,
        results: tuple[HistoryProjection, ...],
    ) -> MutationResult:
        if len(results) != 1:
            raise ValueError("tool result transaction requires one projection")
        projection = results[0]
        result = await self._record_projection(context, projection, consume_inference=True)
        if result.status in {MutationStatus.APPLIED, MutationStatus.ALREADY_APPLIED}:
            self._checkpoint.discard()
        return result

    async def reject_output(self, context: ExecutionOperationContext, rejection: HistoryProjection) -> MutationResult:
        result = await self._record_projection(context, rejection, consume_inference=True)
        if result.status in {MutationStatus.APPLIED, MutationStatus.ALREADY_APPLIED}:
            self._checkpoint.discard()
        return result

    async def stage_accepted_output(
        self,
        context: ExecutionOperationContext,
        output: AcceptedOutput[OutputT],
        history: HistoryProjection,
    ) -> MutationResult:
        prior = self._prior(context)
        if prior is not None:
            return prior
        invalid = self._validate(context)
        if invalid is not None:
            return invalid
        if self._staged is not None and self._staged != output:
            return self._store_conflict(context, "a different accepted output is already staged")
        await self._memory.add_batch(list(history.messages))
        self._checkpoint.record_result()
        self._staged = output
        result = await self._apply(context, reference_id=output.candidate_id)
        return result

    async def commit_terminal_output(
        self, context: ExecutionOperationContext, staged_output_id: str
    ) -> CommittedOutput[OutputT] | MutationResult:
        prior = self._operations.get(context.operation_id)
        if prior is not None:
            return prior
        invalid = self._validate(context)
        if invalid is not None:
            return invalid
        if self._staged is None or self._staged.candidate_id != staged_output_id:
            return self._store_conflict(context, "accepted output is not staged")
        engine_staged = self._output_engine.staged_output
        if engine_staged != self._staged:
            return self._store_conflict(context, "output engine staged record does not match transaction")
        committed = await self._output_engine.commit()
        self._checkpoint.discard()
        self._revision += 1
        self._terminal = committed
        self._operations[context.operation_id] = committed
        return committed

    async def recover_frontier(self, run_id: str) -> ExecutionRecoveryFrontier:
        if run_id != self.run_id:
            return ExecutionRecoveryFrontier(revision=0, cancelled=True)
        return ExecutionRecoveryFrontier(
            revision=self._revision,
            staged_output_id=self._staged.candidate_id if self._staged is not None else "",
            terminal_committed=self._terminal is not None,
            cancelled=self._cancelled,
        )

    async def _record_projection(
        self,
        context: ExecutionOperationContext,
        projection: HistoryProjection,
        *,
        consume_inference: bool = False,
    ) -> MutationResult:
        prior = self._prior(context)
        if prior is not None:
            return prior
        invalid = self._validate(context)
        if invalid is not None:
            return invalid
        await self._memory.add_batch(list(projection.messages))
        if consume_inference:
            self._checkpoint.record_result()
        return await self._apply(context, reference_id=projection.fingerprint)

    async def _apply(self, context: ExecutionOperationContext, *, reference_id: str) -> MutationResult:
        await self._drain_writes()
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
