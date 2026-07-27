from __future__ import annotations

from dataclasses import dataclass

import pytest

from mote.kernel.flow.graph import (
    AgentGraph,
    EffectKind,
    End,
    GraphRunner,
    GraphStepLimitError,
    GraphStructureError,
    NodeId,
    Transition,
)


@dataclass
class State:
    visits: list[NodeId]


class Node:
    def __init__(self, node_id: NodeId, allowed_targets: set[NodeId], outcome) -> None:
        self.node_id = node_id
        self.effect_kind = EffectKind.PURE
        self.allowed_targets = frozenset(allowed_targets)
        self._outcome = outcome

    async def run(self, state: State):
        state.visits.append(self.node_id)
        return self._outcome


@pytest.mark.asyncio
async def test_runner_follows_validated_transitions_to_end():
    graph = AgentGraph(
        start=NodeId.RESTORE,
        nodes={
            NodeId.RESTORE: Node(NodeId.RESTORE, {NodeId.OBSERVE}, Transition(NodeId.OBSERVE)),
            NodeId.OBSERVE: Node(NodeId.OBSERVE, set(), End("done")),
        },
    )
    state = State([])

    assert await GraphRunner(graph).run(state) == "done"
    assert state.visits == [NodeId.RESTORE, NodeId.OBSERVE]


def test_graph_rejects_missing_start_node():
    with pytest.raises(GraphStructureError, match="start node is not registered"):
        AgentGraph(start=NodeId.RESTORE, nodes={})


def test_graph_rejects_mismatched_node_key():
    with pytest.raises(GraphStructureError, match="does not match"):
        AgentGraph(
            start=NodeId.RESTORE,
            nodes={NodeId.RESTORE: Node(NodeId.OBSERVE, set(), End(None))},
        )


def test_graph_rejects_edge_to_missing_node():
    with pytest.raises(GraphStructureError, match="targets missing nodes"):
        AgentGraph(
            start=NodeId.RESTORE,
            nodes={
                NodeId.RESTORE: Node(NodeId.RESTORE, {NodeId.OBSERVE}, Transition(NodeId.OBSERVE)),
            },
        )


@pytest.mark.asyncio
async def test_runner_rejects_transition_not_declared_by_node():
    graph = AgentGraph(
        start=NodeId.RESTORE,
        nodes={
            NodeId.RESTORE: Node(NodeId.RESTORE, set(), Transition(NodeId.OBSERVE)),
        },
    )

    with pytest.raises(GraphStructureError, match="illegal transition"):
        await GraphRunner(graph).run(State([]))


@pytest.mark.asyncio
async def test_runner_stops_unbounded_cycle():
    graph = AgentGraph(
        start=NodeId.RESTORE,
        nodes={
            NodeId.RESTORE: Node(NodeId.RESTORE, {NodeId.RESTORE}, Transition(NodeId.RESTORE)),
        },
    )

    with pytest.raises(GraphStepLimitError, match="exceeded 2 transitions"):
        await GraphRunner(graph, max_steps=2).run(State([]))


def test_runner_requires_positive_step_bound():
    graph = AgentGraph(
        start=NodeId.RESTORE,
        nodes={NodeId.RESTORE: Node(NodeId.RESTORE, set(), End(None))},
    )

    with pytest.raises(ValueError, match="max_steps must be positive"):
        GraphRunner(graph, max_steps=0)
