"""Output-only review/refine topology over the shared flow runtime."""

from __future__ import annotations

from typing import Any

from mote.contracts.completion import CompletionKind
from mote.kernel.flow.graph.core import AgentGraph, EffectKind, End, NodeId, Transition
from mote.kernel.flow.graph.nodes import (
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


class ReviewInterpretNode(FlowNode):
    """Accept only terminal candidates; tool actions are outside this graph."""

    node_id = NodeId.INTERPRET
    effect_kind = EffectKind.PURE
    allowed_targets = frozenset({NodeId.VALIDATE_OUTPUT})

    async def run(
        self,
        state: FlowState[Any],
    ) -> Transition | End[FlowResult[Any] | None]:
        completion = await self.services.completion_policy.evaluate(state.turn)
        if completion.kind is CompletionKind.FAIL:
            raise RuntimeError(completion.reason or "completion policy rejected model turn")
        if completion.kind is not CompletionKind.VALIDATE_CANDIDATE:
            raise RuntimeError("review/refine graph requires a terminal output candidate; tool actions are disabled")
        state.turn = (state.turn, completion.candidate_index or 0)
        return Transition(NodeId.VALIDATE_OUTPUT)


def build_review_refine_graph(services: FlowServices) -> AgentGraph[FlowState[Any], FlowResult[Any] | None]:
    """Build the high-assurance output-only review/refine graph."""
    nodes = {
        node.node_id: node
        for node in (
            RestoreNode(services),
            ObserveNode(services),
            BudgetNode(services),
            ThinkNode(services),
            ReviewInterpretNode(services),
            ValidateOutputNode(services),
            WaitBackgroundNode(services),
        )
    }
    return AgentGraph(start=NodeId.RESTORE, nodes=nodes)


__all__ = ["build_review_refine_graph"]
