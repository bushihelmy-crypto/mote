"""Typed output validation and transaction coordination."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Generic, TypeVar

from mote.contracts.conversation import AIMessage, CauseBy, Message
from mote.contracts.execution.models import MutationResult, MutationStatus
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import FinalCandidateAction
from mote.contracts.output import CommittedOutput, OutputEvaluation
from mote.contracts.ports.execution.transaction import ExecutionOutputTransactionPort
from mote.contracts.ports.output.evaluation import OutputEngine
from mote.kernel.commands import CommandChannel
from mote.kernel.execution.context import ExecutionContext
from mote.kernel.execution.result import ExecutionResult
from mote.kernel.inference.base import BaseInferenceEngine

OutputT = TypeVar("OutputT")


class OutputOperation(Generic[OutputT]):
    """Own the accepted-output transaction and correction recording."""

    def __init__(
        self,
        *,
        context: Callable[[], ExecutionContext],
        channel: Callable[[], CommandChannel],
        inference_engine: BaseInferenceEngine,
        transaction: ExecutionOutputTransactionPort[OutputT],
        output_engine: OutputEngine[OutputT],
        report_inference_result: Callable[[InferenceResult], None],
    ) -> None:
        self._context = context
        self._channel = channel
        self._inference_engine = inference_engine
        self._transaction = transaction
        self._output_engine = output_engine
        self._report_think_result = report_inference_result

    async def evaluate(self, candidate: FinalCandidateAction) -> OutputEvaluation[OutputT]:
        return await self._output_engine.evaluate(candidate)

    async def commit(self) -> CommittedOutput[OutputT]:
        staged = self._output_engine.staged_output
        if staged is None:
            raise RuntimeError("accepted output is not staged")
        result = await self._transaction.commit_terminal_output(
            self._transaction.context(f"{staged.candidate_id}:terminal-commit"),
            staged.candidate_id,
        )
        return self._require_committed(result)

    async def accept(self, candidate: FinalCandidateAction) -> Message:
        content = self._inference_engine.result.content or ""
        self._report_think_result(self._inference_engine.result)
        projection = await self._channel().project_output_candidate(
            content,
            candidate,
            accepted=True,
        )
        staged = self._output_engine.staged_output
        if staged is None:
            raise RuntimeError("accepted output evaluation did not produce a staged record")
        result = await self._transaction.stage_accepted_output(
            self._transaction.context(f"{staged.candidate_id}:accepted-stage"),
            staged,
            projection,
        )
        self._require_applied(result)
        await self._inference_engine.join()
        response = AIMessage(
            content=content,
            sent_from=self._context().name,
            cause_by=CauseBy.RUN_COMMAND,
        )
        return response

    async def reject(
        self,
        evaluation: OutputEvaluation[OutputT],
        candidate: FinalCandidateAction,
    ) -> None:
        content = self._inference_engine.result.content or ""
        self._report_think_result(self._inference_engine.result)
        feedback = evaluation.feedback() if evaluation.correction_allowed else None
        projection = await self._channel().project_output_candidate(
            content,
            candidate,
            accepted=False,
            feedback=feedback,
        )
        result = await self._transaction.reject_output(
            self._transaction.context(f"{evaluation.candidate_id}:output-rejection"),
            projection,
        )
        self._require_applied(result)
        await self._inference_engine.join()

    async def restore(self) -> ExecutionResult[OutputT] | None:
        if not self._output_engine.has_restored_terminal_output:
            return None
        accepted_value = self._output_engine.accepted_value
        if accepted_value is None:
            raise RuntimeError("restored terminal output has no accepted value")
        encoded = self._output_engine.contract.decoder.encode(accepted_value)
        content = (
            encoded
            if isinstance(encoded, str)
            else json.dumps(encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        response = AIMessage(
            content=content,
            sent_from=self._context().name,
            cause_by=CauseBy.RUN_COMMAND,
        )
        committed = await self.commit()
        return ExecutionResult(presentation=response, committed_output=committed)

    @staticmethod
    def _require_applied(result: MutationResult) -> None:
        if result.status not in {
            MutationStatus.APPLIED,
            MutationStatus.ALREADY_APPLIED,
        }:
            raise RuntimeError(result.reason or result.status.value)

    @staticmethod
    def _require_committed(
        result: CommittedOutput[OutputT] | MutationResult,
    ) -> CommittedOutput[OutputT]:
        if isinstance(result, MutationResult):
            OutputOperation._require_applied(result)
            raise RuntimeError("terminal commit returned no committed output")
        return result


__all__ = ["OutputOperation"]
