"""Reusable built-in execution nodes over narrow dependencies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from mote.contracts.conversation import AIMessage, CauseBy, MessagePriority
from mote.contracts.model.turn import ModelTurn
from mote.contracts.output.errors import OutputCorrectionExhaustedError
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
from mote.kernel.execution.state import CandidateSelection, ExecutionState, NoModelTurn
from mote.kernel.inference.base import BaseInferenceEngine

OutputT = TypeVar("OutputT")


class ExecutionNode:
    node_id: NodeId
    allowed_targets: frozenset[NodeId]


class RestoreNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.RESTORE
    effect_kind = EffectKind.LEDGERED
    allowed_targets = frozenset({NodeId.OBSERVE})

    def __init__(self, outputs: OutputOperation[OutputT]) -> None:
        self._outputs = outputs

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        restored = await self._outputs.restore()
        return End(restored) if restored is not None else Transition(NodeId.OBSERVE)


class ObserveNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.OBSERVE
    effect_kind = EffectKind.LEDGERED
    allowed_targets = frozenset({NodeId.BUDGET})

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
            if not await self._observation.observe():
                return End(None)
            state.initial_observe_complete = True
            self._set_active(True)
        elif await self._observation.observe(max_priority=MessagePriority.NEXT, interjection=True):
            self._set_active(True)
        return Transition(NodeId.BUDGET)


class BudgetNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.BUDGET
    effect_kind = EffectKind.PURE
    allowed_targets = frozenset({NodeId.THINK})

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
            return End(ExecutionResult(presentation=state.response, committed_output=state.committed_output))
        if self._advance_turn is not None:
            self._advance_turn()
        return Transition(NodeId.THINK)


class InferenceNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.THINK
    effect_kind = EffectKind.REPLAYABLE
    allowed_targets = frozenset({NodeId.INTERPRET, NodeId.WAIT_BACKGROUND})

    def __init__(
        self,
        inference: InferenceService,
        current_channel: Callable[[], CommandChannel],
        inference_engine: BaseInferenceEngine,
        get_bg_pool: Callable[[], BackgroundTaskService | None],
    ) -> None:
        self._inference = inference
        self._current_channel = current_channel
        self._inference_engine = inference_engine
        self._get_bg_pool = get_bg_pool

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        if await self._inference.infer():
            state.turn = await self._current_channel().model_turn(self._inference_engine.result)
            return Transition(NodeId.INTERPRET)
        bg_pool = self._get_bg_pool()
        if bg_pool and bg_pool.has_pending():
            return Transition(NodeId.WAIT_BACKGROUND)
        return End(ExecutionResult(presentation=state.response, committed_output=state.committed_output))


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
        if not isinstance(state.turn, ModelTurn):
            raise RuntimeError("action phase requires a model turn")
        state.response = await self._actions.execute(state.turn)
        state.turn = NoModelTurn()
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
        if not isinstance(state.turn, CandidateSelection):
            raise RuntimeError("output validation requires a candidate selection")
        candidate = state.turn.turn.final_candidates[state.turn.candidate_index]
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
            return Transition(NodeId.OBSERVE)
        state.response = await self._outputs.accept(candidate)
        state.committed_output = await self._outputs.commit()
        return End(ExecutionResult(presentation=state.response, committed_output=state.committed_output))


class WaitBackgroundNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.WAIT_BACKGROUND
    effect_kind = EffectKind.WAITABLE
    allowed_targets = frozenset({NodeId.OBSERVE})

    def __init__(
        self,
        get_bg_pool: Callable[[], BackgroundTaskService | None],
    ) -> None:
        self._get_bg_pool = get_bg_pool

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        bg_pool = self._get_bg_pool()
        if bg_pool and bg_pool.has_pending():
            await bg_pool.wait_any()
        return Transition(NodeId.OBSERVE)


__all__ = [
    "ActNode",
    "BudgetNode",
    "ExecutionNode",
    "ObserveNode",
    "RestoreNode",
    "InferenceNode",
    "ValidateOutputNode",
    "WaitBackgroundNode",
]
