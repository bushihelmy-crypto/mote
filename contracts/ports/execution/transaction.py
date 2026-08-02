"""Execution transaction persistence port."""

from typing import Protocol, TypeVar

from mote.contracts.conversation import Message
from mote.contracts.execution.models import ExecutionOperationContext, ExecutionRecoveryFrontier, MutationResult
from mote.contracts.output import AcceptedOutput, CommittedOutput

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

    async def record_model_turn(
        self, context: ExecutionOperationContext, turn: HistoryProjection
    ) -> MutationResult: ...

    async def record_tool_results(
        self,
        context: ExecutionOperationContext,
        results: tuple[HistoryProjection, ...],
    ) -> MutationResult: ...

    async def reject_output(
        self, context: ExecutionOperationContext, rejection: HistoryProjection
    ) -> MutationResult: ...

    async def recover_frontier(self, run_id: str) -> ExecutionRecoveryFrontier: ...


class OutputTransactionPort(Protocol[OutputT]):
    def context(self, operation_id: str, *, attempt_id: str = "") -> ExecutionOperationContext: ...

    async def stage_accepted_output(
        self,
        context: ExecutionOperationContext,
        output: AcceptedOutput[OutputT],
        history: HistoryProjection,
    ) -> MutationResult: ...

    async def commit_terminal_output(
        self, context: ExecutionOperationContext, staged_output_id: str
    ) -> CommittedOutput[OutputT] | MutationResult: ...


class ExecutionOutputTransactionPort(ExecutionTransactionPort, OutputTransactionPort[OutputT], Protocol[OutputT]):
    pass


__all__ = [
    "ExecutionOutputTransactionPort",
    "ExecutionTransactionPort",
    "HistoryProjection",
    "OutputTransactionPort",
]
