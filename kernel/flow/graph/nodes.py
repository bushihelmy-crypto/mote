"""Reusable built-in flow nodes over the shared domain services."""

from __future__ import annotations

from typing import Any

from mote.contracts.errors.output import OutputCorrectionExhaustedError
from mote.contracts.schema import AIMessage, CauseBy, MessagePriority
from mote.kernel.flow.graph.core import EffectKind, End, NodeId, Transition
from mote.kernel.flow.result import FlowResult
from mote.kernel.flow.services.container import FlowServices
from mote.kernel.flow.state import FlowState


class FlowNode:
    node_id: NodeId
    allowed_targets: frozenset[NodeId]

    def __init__(self, services: FlowServices) -> None:
        self.services = services


class RestoreNode(FlowNode):
    node_id = NodeId.RESTORE
    effect_kind = EffectKind.LEDGERED
    allowed_targets = frozenset({NodeId.OBSERVE})

    async def run(
        self,
        state: FlowState[Any],
    ) -> Transition | End[FlowResult[Any] | None]:
        restored = await self.services.outputs.restore()
        return End(restored) if restored is not None else Transition(NodeId.OBSERVE)


class ObserveNode(FlowNode):
    node_id = NodeId.OBSERVE
    effect_kind = EffectKind.LEDGERED
    allowed_targets = frozenset({NodeId.BUDGET})

    async def run(
        self,
        state: FlowState[Any],
    ) -> Transition | End[FlowResult[Any] | None]:
        if not state.initial_observe_complete:
            if not await self.services.observation.observe():
                return End(None)
            state.initial_observe_complete = True
            self.services.set_active(True)
        elif await self.services.observation.observe(max_priority=MessagePriority.NEXT, interjection=True):
            self.services.set_active(True)
        return Transition(NodeId.BUDGET)


class BudgetNode(FlowNode):
    node_id = NodeId.BUDGET
    effect_kind = EffectKind.PURE
    allowed_targets = frozenset({NodeId.THINK})

    async def run(
        self,
        state: FlowState[Any],
    ) -> Transition | End[FlowResult[Any] | None]:
        verdict = await self.services.context_provider.enforce_budget()
        if verdict.stop:
            state.response = AIMessage(
                content=verdict.message,
                sent_from=self.services.context().name,
                cause_by=CauseBy.RUN_COMMAND,
            )
            return End(FlowResult(presentation=state.response, committed_output=state.committed_output))
        if self.services.advance_turn is not None:
            self.services.advance_turn()
        return Transition(NodeId.THINK)


class ThinkNode(FlowNode):
    node_id = NodeId.THINK
    effect_kind = EffectKind.REPLAYABLE
    allowed_targets = frozenset({NodeId.INTERPRET, NodeId.WAIT_BACKGROUND})

    async def run(
        self,
        state: FlowState[Any],
    ) -> Transition | End[FlowResult[Any] | None]:
        if await self.services.think.think():
            state.turn = await self.services.current_channel().model_turn(self.services.think_engine)
            return Transition(NodeId.INTERPRET)
        bg_pool = self.services.get_bg_pool()
        if bg_pool and bg_pool.has_pending():
            return Transition(NodeId.WAIT_BACKGROUND)
        return End(FlowResult(presentation=state.response, committed_output=state.committed_output))


class ActNode(FlowNode):
    node_id = NodeId.ACT
    effect_kind = EffectKind.EXTERNAL
    allowed_targets = frozenset({NodeId.OBSERVE})

    async def run(
        self,
        state: FlowState[Any],
    ) -> Transition | End[FlowResult[Any] | None]:
        state.response = await self.services.actions.execute(state.turn)
        state.turn = None
        return Transition(NodeId.OBSERVE)


class ValidateOutputNode(FlowNode):
    node_id = NodeId.VALIDATE_OUTPUT
    effect_kind = EffectKind.LEDGERED
    allowed_targets = frozenset({NodeId.OBSERVE})

    async def run(
        self,
        state: FlowState[Any],
    ) -> Transition | End[FlowResult[Any] | None]:
        turn, candidate_index = state.turn
        candidate = turn.final_candidates[candidate_index]
        evaluation = await self.services.outputs.evaluate(candidate)
        candidate = candidate.model_copy(update={"candidate_id": evaluation.candidate_id})
        if not evaluation.accepted:
            await self.services.outputs.reject(evaluation, candidate)
            if not evaluation.correction_allowed:
                raise OutputCorrectionExhaustedError(
                    max_corrections=evaluation.max_corrections,
                    candidate_id=evaluation.candidate_id,
                    issues=evaluation.issues,
                )
            state.turn = None
            return Transition(NodeId.OBSERVE)
        state.response = await self.services.outputs.accept(candidate)
        state.committed_output = await self.services.outputs.commit()
        return End(FlowResult(presentation=state.response, committed_output=state.committed_output))


class WaitBackgroundNode(FlowNode):
    node_id = NodeId.WAIT_BACKGROUND
    effect_kind = EffectKind.WAITABLE
    allowed_targets = frozenset({NodeId.OBSERVE})

    async def run(
        self,
        state: FlowState[Any],
    ) -> Transition | End[FlowResult[Any] | None]:
        bg_pool = self.services.get_bg_pool()
        if bg_pool and bg_pool.has_pending():
            await bg_pool.wait_any()
        return Transition(NodeId.OBSERVE)


__all__ = [
    "ActNode",
    "BudgetNode",
    "FlowNode",
    "ObserveNode",
    "RestoreNode",
    "ThinkNode",
    "ValidateOutputNode",
    "WaitBackgroundNode",
]
