"""Execution transaction persistence port."""

from typing import Protocol, TypeVar

from mote.contracts.conversation import Message
from mote.contracts.execution.models import ExecutionOperationContext, ExecutionRecoveryFrontier, MutationResult
from mote.contracts.output import CommittedOutput, ValidatedCandidate

OutputT = TypeVar("OutputT")


class HistoryProjection(Protocol):
    @property
    def messages(self) -> tuple[Message, ...]: ...

    @property
    def fingerprint(self) -> str: ...


class ExecutionTransactionPort(Protocol):
    def context(self, operation_id: str, *, attempt_id: str = "") -> ExecutionOperationContext: ...

    async def record_history(
        self, context: ExecutionOperationContext, history: HistoryProjection
    ) -> MutationResult: ...

    async def record_effect_intent(
        self, context: ExecutionOperationContext, projection: HistoryProjection
    ) -> MutationResult: ...

    async def settle_effect_batch(
        self, context: ExecutionOperationContext, projection: HistoryProjection
    ) -> MutationResult: ...

    async def record_local_action_batch(
        self, context: ExecutionOperationContext, projection: HistoryProjection
    ) -> MutationResult: ...

    async def reject_output(
        self, context: ExecutionOperationContext, rejection: HistoryProjection
    ) -> MutationResult: ...

    async def recover_frontier(self, run_id: str) -> ExecutionRecoveryFrontier: ...


class OutputTransactionPort(Protocol[OutputT]):
    def context(self, operation_id: str, *, attempt_id: str = "") -> ExecutionOperationContext: ...

    async def commit_final_output(
        self,
        context: ExecutionOperationContext,
        output: ValidatedCandidate[OutputT],
        message: Message,
    ) -> CommittedOutput[OutputT] | MutationResult: ...


class ExecutionOutputTransactionPort(ExecutionTransactionPort, OutputTransactionPort[OutputT], Protocol[OutputT]):
    pass


__all__ = [
    "ExecutionOutputTransactionPort",
    "ExecutionTransactionPort",
    "HistoryProjection",
    "OutputTransactionPort",
]
