"""The built-in ReAct graph definition."""

from __future__ import annotations

from typing import Generic, TypeVar

from mote.contracts.model.turn import ModelTurn
from mote.contracts.output.completion import CompletionKind
from mote.contracts.ports.execution.model_turn_completion import ModelTurnCompletionPolicy
from mote.kernel.execution.graph.core import AgentGraph, EffectKind, End, NodeId, Transition
from mote.kernel.execution.graph.nodes import (
    ActNode,
    AwaitQuiescenceNode,
    BudgetNode,
    ExecutionNode,
    InferenceNode,
    ObserveNode,
    RestoreNode,
    ValidateOutputNode,
)
from mote.kernel.execution.operations.container import GraphAssemblyInputs
from mote.kernel.execution.result import ExecutionResult
from mote.kernel.execution.state import ExecutionState, PendingCandidate

OutputT = TypeVar("OutputT")


class ReActInterpretNode(ExecutionNode, Generic[OutputT]):
    node_id = NodeId.INTERPRET
    effect_kind = EffectKind.PURE
    allowed_targets = frozenset({NodeId.ACT, NodeId.AWAIT_QUIESCENCE})

    def __init__(self, completion_policy: ModelTurnCompletionPolicy) -> None:
        self._completion_policy = completion_policy

    async def run(
        self,
        state: ExecutionState[OutputT],
    ) -> Transition | End[ExecutionResult[OutputT] | None]:
        if not isinstance(state.turn, ModelTurn):
            raise RuntimeError("interpretation requires a model turn")
        completion = await self._completion_policy.evaluate(state.turn)
        if completion.kind is CompletionKind.FAIL:
            raise RuntimeError(completion.reason or "completion policy rejected model turn")
        if completion.kind is CompletionKind.VALIDATE_CANDIDATE:
            candidate_index = completion.candidate_index
            if candidate_index is None:
                raise RuntimeError("validated completion is missing its candidate index")
            state.turn = PendingCandidate(state.turn, candidate_index)
            return Transition(NodeId.AWAIT_QUIESCENCE)
        return Transition(NodeId.ACT)


def build_react_graph(
    inputs: GraphAssemblyInputs[OutputT],
) -> AgentGraph[ExecutionState[OutputT], ExecutionResult[OutputT] | None]:
    nodes = {
        node.node_id: node
        for node in (
            RestoreNode(inputs.restore, allow_pending_act=True),
            ObserveNode(inputs.observation, inputs.set_active),
            BudgetNode(inputs.context_provider, inputs.context, inputs.advance_turn),
            InferenceNode(inputs.inference, inputs.current_channel, inputs.inference_engine),
            ReActInterpretNode(inputs.completion_policy),
            ActNode(inputs.actions),
            ValidateOutputNode(inputs.outputs),
            AwaitQuiescenceNode(inputs.inbox_activity, inputs.get_bg_pool),
        )
    }
    return AgentGraph(start=NodeId.RESTORE, nodes=nodes)


__all__ = ["build_react_graph"]
