"""The built-in ReAct graph definition."""

from __future__ import annotations

from typing import Any

from mote.contracts.completion import CompletionKind
from mote.kernel.flow.graph.core import AgentGraph, EffectKind, End, NodeId, Transition
from mote.kernel.flow.graph.nodes import (
    ActNode,
    BudgetNode,
    FlowNode,
    ObserveNode,
    RestoreNode,
    ThinkNode,
    ValidateOutputNode,
    WaitBackgroundNode,
)
from mote.kernel.flow.result import FlowResult
from mote.kernel.flow.services.container import FlowServices
from mote.kernel.flow.state import FlowState


class ReActInterpretNode(FlowNode):
    node_id = NodeId.INTERPRET
    effect_kind = EffectKind.PURE
    allowed_targets = frozenset({NodeId.ACT, NodeId.VALIDATE_OUTPUT})

    async def run(
        self,
        state: FlowState[Any],
    ) -> Transition | End[FlowResult[Any] | None]:
        completion = await self.services.completion_policy.evaluate(state.turn)
        if completion.kind is CompletionKind.FAIL:
            raise RuntimeError(completion.reason or "completion policy rejected model turn")
        if completion.kind is CompletionKind.VALIDATE_CANDIDATE:
            state.turn = (state.turn, completion.candidate_index or 0)
            return Transition(NodeId.VALIDATE_OUTPUT)
        return Transition(NodeId.ACT)


def build_react_graph(services: FlowServices) -> AgentGraph[FlowState[Any], FlowResult[Any] | None]:
    nodes = {
        node.node_id: node
        for node in (
            RestoreNode(services),
            ObserveNode(services),
            BudgetNode(services),
            ThinkNode(services),
            ReActInterpretNode(services),
            ActNode(services),
            ValidateOutputNode(services),
            WaitBackgroundNode(services),
        )
    }
    return AgentGraph(start=NodeId.RESTORE, nodes=nodes)


__all__ = ["build_react_graph"]
