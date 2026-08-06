"""Reusable built-in execution nodes over narrow dependencies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from mote.contracts.conversation import AIMessage, CauseBy, MessagePriority
from mote.contracts.execution.models import InferenceCompleted
from mote.contracts.execution.restore import (
    CommittedExecution,
    ExecutionRestorePort,
    ExternalEffectReconciliationRequired,
    InDoubtExecution,
    InterruptedExecution,
    InterruptedExecutionNeedsSettlement,
    NoPendingExecution,
    ObserveExecution,
    PendingActExecution,
    UnrecoverablePreV1Execution,
)
from mote.contracts.model.turn import ModelTurn
from mote.contracts.output.errors import OutputCorrectionExhaustedError
from mote.contracts.ports.conversation.message_activity import MessageActivity
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.kernel.commands import CommandChannel
from mote.kernel.execution.context import ExecutionContext
from mote.kernel.execution.context_provider import BaseContextProvider
from mote.kernel.execution.graph.core import EffectKind, End, NodeId, Transition
from mote.kernel.execution.operations.action_execution import ActionExecutionService
from mote.kernel.execution.operations.inference import InferenceService
from mote.kernel.execution.operations.observation import ObservationService
from mote.kernel.execution.operations.output import OutputOperation
from mote.kernel.execution.result import ExecutionResult
from mote.kernel.execution.state import ExecutionState, NoModelTurn, PendingCandidate, RecoveredPendingAct
from mote.kernel.inference.base import BaseInferenceEngine

OutputT = TypeVar("OutputT")


class ExecutionNode:
    node_id: NodeId
    allowed_targets: frozenset[NodeId]


class RestoreNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.RESTORE
    effect_kind = EffectKind.LEDGERED

    def __init__(
        self,
        restore: ExecutionRestorePort[OutputT],
        *,
        allow_pending_act: bool,
    ) -> None:
        self._restore = restore
        self.allowed_targets = (
            frozenset({NodeId.ACT, NodeId.OBSERVE}) if allow_pending_act else frozenset({NodeId.OBSERVE})
        )
        self._allow_pending_act = allow_pending_act

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        execution = self._restore.snapshot()
        if isinstance(execution, CommittedExecution):
            return End(
                ExecutionResult(
                    presentation=execution.presentation,
                    committed_output=execution.result,
                )
            )
        if isinstance(execution, PendingActExecution):
            if not self._allow_pending_act:
                raise RuntimeError("execution topology cannot recover a PendingAct frontier")
            state.turn = RecoveredPendingAct(execution.frontier)
            return Transition(NodeId.ACT)
        if isinstance(execution, InDoubtExecution):
            identities = ", ".join(item.value for item in execution.invocation_ids)
            raise RuntimeError(f"external effect outcome requires reconciliation before resume: {identities}")
        if isinstance(execution, ExternalEffectReconciliationRequired):
            identities = ", ".join(item.value for item in execution.invocation_ids)
            raise RuntimeError(
                "started external effect requires receipt-only reconciliation " f"before resume: {identities}"
            )
        if isinstance(execution, InterruptedExecution):
            return End(None)
        if isinstance(execution, InterruptedExecutionNeedsSettlement):
            raise RuntimeError(f"interrupted run requires receipt-only settlement before resume: {execution.run_id}")
        if isinstance(execution, UnrecoverablePreV1Execution):
            raise RuntimeError(execution.code)
        if isinstance(execution, ObserveExecution):
            state.initial_observe_complete = True
            state.continue_inference = execution.cursor.continue_inference
            return Transition(NodeId.OBSERVE)
        if isinstance(execution, NoPendingExecution):
            return Transition(NodeId.OBSERVE)
        raise TypeError("unknown execution restore disposition")


class ObserveNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.OBSERVE
    effect_kind = EffectKind.LEDGERED
    allowed_targets = frozenset({NodeId.BUDGET, NodeId.AWAIT_QUIESCENCE})

    def __init__(
        self,
        observation: ObservationService,
        set_active: Callable[[bool], None],
    ) -> None:
        self._observation = observation
        self._set_active = set_active

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        if not state.initial_observe_complete:
            observed = await self._observation.observe()
            if not observed.observed_count:
                state.requested_end = None
                return Transition(NodeId.AWAIT_QUIESCENCE)
            state.initial_observe_complete = True
            if observed.user_message_count:
                self._set_active(True)
        else:
            observed = await self._observation.observe(max_priority=MessagePriority.NEXT, interjection=True)
            if not observed.observed_count:
                if state.continue_inference:
                    state.continue_inference = False
                    return Transition(NodeId.BUDGET)
                return Transition(NodeId.AWAIT_QUIESCENCE)
        if observed.user_message_count:
            self._set_active(True)
        return Transition(NodeId.BUDGET)


class BudgetNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.BUDGET
    effect_kind = EffectKind.PURE
    allowed_targets = frozenset({NodeId.THINK, NodeId.AWAIT_QUIESCENCE})

    def __init__(
        self,
        context_provider: BaseContextProvider,
        context: Callable[[], ExecutionContext],
        advance_turn: Callable[[], int] | None,
    ) -> None:
        self._context_provider = context_provider
        self._context = context
        self._advance_turn = advance_turn

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        verdict = await self._context_provider.enforce_budget()
        if verdict.stop:
            state.response = AIMessage(
                content=verdict.message,
                sent_from=self._context().name,
                cause_by=CauseBy.RUN_COMMAND,
            )
            state.requested_end = ExecutionResult(
                presentation=state.response,
                committed_output=state.committed_output,
            )
            return Transition(NodeId.AWAIT_QUIESCENCE)
        if self._advance_turn is not None:
            self._advance_turn()
        return Transition(NodeId.THINK)


class InferenceNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.THINK
    effect_kind = EffectKind.REPLAYABLE
    allowed_targets = frozenset({NodeId.INTERPRET, NodeId.AWAIT_QUIESCENCE})

    def __init__(
        self,
        inference: InferenceService,
        current_channel: Callable[[], CommandChannel],
        inference_engine: BaseInferenceEngine,
    ) -> None:
        self._inference = inference
        self._current_channel = current_channel
        self._inference_engine = inference_engine

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        disposition = await self._inference.infer()
        if isinstance(disposition, InferenceCompleted):
            state.turn = await self._current_channel().model_turn(self._inference_engine.result)
            return Transition(NodeId.INTERPRET)
        state.requested_end = ExecutionResult(
            presentation=state.response,
            committed_output=state.committed_output,
        )
        return Transition(NodeId.AWAIT_QUIESCENCE)


class ActNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.ACT
    effect_kind = EffectKind.EXTERNAL
    allowed_targets = frozenset({NodeId.OBSERVE})

    def __init__(self, actions: ActionExecutionService) -> None:
        self._actions = actions

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        if isinstance(state.turn, RecoveredPendingAct):
            state.response = await self._actions.resume(state.turn.frontier)
        elif isinstance(state.turn, ModelTurn):
            state.response = await self._actions.execute(state.turn)
        else:
            raise RuntimeError("action phase requires a model turn or recovered PendingAct")
        state.turn = NoModelTurn()
        state.continue_inference = True
        return Transition(NodeId.OBSERVE)


class ValidateOutputNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.VALIDATE_OUTPUT
    effect_kind = EffectKind.LEDGERED
    allowed_targets = frozenset({NodeId.OBSERVE})

    def __init__(self, outputs: OutputOperation[OutputT]) -> None:
        self._outputs = outputs

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        if not isinstance(state.turn, PendingCandidate):
            raise RuntimeError("output validation requires a candidate selection")
        candidate = state.turn.candidate
        evaluation = await self._outputs.evaluate(candidate)
        candidate = candidate.model_copy(update={"candidate_id": evaluation.candidate_id})
        if not evaluation.accepted:
            await self._outputs.reject(evaluation, candidate)
            if not evaluation.correction_allowed:
                raise OutputCorrectionExhaustedError(
                    max_corrections=evaluation.max_corrections,
                    candidate_id=evaluation.candidate_id,
                    issues=evaluation.issues,
                )
            state.turn = NoModelTurn()
            state.continue_inference = True
            return Transition(NodeId.OBSERVE)
        state.response, state.committed_output = await self._outputs.validate_and_commit(candidate)
        return End(ExecutionResult(presentation=state.response, committed_output=state.committed_output))


class AwaitQuiescenceNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.AWAIT_QUIESCENCE
    effect_kind = EffectKind.WAITABLE
    allowed_targets = frozenset({NodeId.OBSERVE, NodeId.VALIDATE_OUTPUT})

    def __init__(
        self,
        inbox_activity: Callable[[], MessageActivity],
        get_bg_pool: Callable[[], BackgroundTaskService | None],
    ) -> None:
        self._inbox_activity = inbox_activity
        self._get_bg_pool = get_bg_pool

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        inbox = self._inbox_activity()
        while True:
            before = inbox.activity_snapshot()
            if before.pending:
                self._invalidate_candidate(state)
                return Transition(NodeId.OBSERVE)
            pool = self._get_bg_pool()
            snapshot = pool.pin_snapshot(owner=pool.owner) if pool is not None else None
            if snapshot is not None and snapshot.pin_count:
                await inbox.wait_for_activity(before.generation)
                self._invalidate_candidate(state)
                return Transition(NodeId.OBSERVE)
            after = inbox.activity_snapshot()
            if after.pending or after.generation != before.generation:
                continue
            if isinstance(state.turn, PendingCandidate):
                return Transition(NodeId.VALIDATE_OUTPUT)
            return End(state.requested_end)

    @staticmethod
    def _invalidate_candidate(state: ExecutionState[OutputT]) -> None:
        state.turn = NoModelTurn()
        state.requested_end = None


__all__ = [
    "ActNode",
    "BudgetNode",
    "ExecutionNode",
    "ObserveNode",
    "RestoreNode",
    "InferenceNode",
    "ValidateOutputNode",
    "AwaitQuiescenceNode",
]
