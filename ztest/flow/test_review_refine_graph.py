"""Contracts for the output-only ReviewRefineGraph."""

from __future__ import annotations

import pytest

from mote.contracts.schema import UserMessage
from mote.kernel.flow.graph import NodeId, build_react_graph, build_review_refine_graph
from mote.kernel.flow.graph.nodes import (
    BudgetNode,
    ObserveNode,
    RestoreNode,
    ThinkNode,
    ValidateOutputNode,
    WaitBackgroundNode,
)

from .conftest import FakeChannel, FakeExecutor, FakeThinkEngine

pytestmark = pytest.mark.asyncio


def _news(bundle):
    bundle.buffer.push(UserMessage("produce a reviewed answer", send_to={"Alice"}))


async def test_review_refine_runs_on_the_same_engine_without_tools(make_engine):
    executor = FakeExecutor()
    bundle = make_engine(
        think_engine=FakeThinkEngine(content="reviewed answer"),
        channel=FakeChannel(terminal=True),
        executor=executor,
        graph_builder=build_review_refine_graph,
    )
    _news(bundle)

    result = await bundle.engine.run()

    assert result is not None
    assert result.presentation.content == "reviewed answer"
    assert executor.calls == []


async def test_review_refine_rejects_tool_actions_at_the_topology_boundary(make_engine):
    bundle = make_engine(
        channel=FakeChannel(commands=[{"id": "t1", "command_name": "Read", "args": {}}]),
        graph_builder=build_review_refine_graph,
    )
    _news(bundle)

    with pytest.raises(RuntimeError, match="tool actions are disabled"):
        await bundle.engine.run()

    assert bundle.executor.calls == []


async def test_built_in_graphs_share_domain_nodes_and_runner_contract(make_engine):
    bundle = make_engine()
    services = bundle.engine._services
    react = build_react_graph(services)
    review = build_review_refine_graph(services)
    shared = {
        NodeId.RESTORE: RestoreNode,
        NodeId.OBSERVE: ObserveNode,
        NodeId.BUDGET: BudgetNode,
        NodeId.THINK: ThinkNode,
        NodeId.VALIDATE_OUTPUT: ValidateOutputNode,
        NodeId.WAIT_BACKGROUND: WaitBackgroundNode,
    }

    assert NodeId.ACT in react.nodes and NodeId.ACT not in review.nodes
    for node_id, node_type in shared.items():
        assert type(react.nodes[node_id]) is node_type
        assert type(review.nodes[node_id]) is node_type
        assert react.nodes[node_id].effect_kind is review.nodes[node_id].effect_kind
