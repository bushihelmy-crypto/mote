from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.contracts.activity import (
    ActivityEdge,
    ActivityKind,
    ActivityNode,
    ActivityNodeKind,
    ActivityNodeState,
    ActivityNodeStatus,
    ActivityOutcome,
    ActivityTopology,
)
from mote.contracts.events.task import ActivityCompletedEvent, ActivityStartedEvent
from mote.product.presentation.events.events import ActivityCompleted, ActivityStarted
from mote.product.presentation.projection.handlers.activity import project_activity_event


def _topology() -> ActivityTopology:
    return ActivityTopology(
        nodes=(
            ActivityNode("collect", ActivityNodeKind.TOOL, "Collect"),
            ActivityNode("merge", ActivityNodeKind.FOLD, "Merge"),
        ),
        edges=(ActivityEdge("collect", "merge"),),
    )


def _state() -> ActivityNodeState:
    return ActivityNodeState(
        node_id="collect",
        kind=ActivityNodeKind.TOOL,
        label="Collect",
        status=ActivityNodeStatus.FAILED,
        attempts=2,
        error="provider failed",
        arguments={"query": "typed activity"},
    )


def test_activity_projection_preserves_canonical_contract_instances() -> None:
    topology = _topology()
    started = project_activity_event(ActivityStartedEvent(activity_kind=ActivityKind.GRAPH, topology=topology))
    assert started is not None
    assert isinstance(started[0], ActivityStarted)
    assert started[0].topology is topology

    state = _state()
    completed = project_activity_event(
        ActivityCompletedEvent(
            outcome=ActivityOutcome.FAILED,
            node_states=(state,),
            summary="failed",
        )
    )
    assert completed is not None
    assert isinstance(completed[0], ActivityCompleted)
    assert completed[0].node_states == (state,)
    assert completed[0].outcome is ActivityOutcome.FAILED


def test_activity_contract_rejects_unknown_status_and_invalid_attempts() -> None:
    with pytest.raises(TypeError, match="ActivityNodeStatus"):
        ActivityNodeState("node", ActivityNodeKind.TOOL, "Node", "unknown", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative"):
        ActivityNodeState("node", ActivityNodeKind.TOOL, "Node", ActivityNodeStatus.FAILED, -1)


def test_activity_node_state_validates_and_freezes_dynamic_fields() -> None:
    arguments = {"items": [1, 2]}
    state = ActivityNodeState(
        "node",
        ActivityNodeKind.TOOL,
        "Node",
        ActivityNodeStatus.FAILED,
        1,
        arguments=arguments,
    )
    arguments["items"].append(3)
    assert state.arguments == {"items": (1, 2)}

    with pytest.raises(TypeError, match="error must be a string"):
        ActivityNodeState(
            "node",
            ActivityNodeKind.TOOL,
            "Node",
            ActivityNodeStatus.FAILED,
            1,
            error=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="not JSON-safe"):
        ActivityNodeState(
            "node",
            ActivityNodeKind.TOOL,
            "Node",
            ActivityNodeStatus.FAILED,
            1,
            arguments={"bad": object()},  # type: ignore[dict-item]
        )


def test_activity_contract_rejects_invalid_topology_shape() -> None:
    with pytest.raises(ValueError, match="unknown node"):
        ActivityTopology(
            nodes=(ActivityNode("known", ActivityNodeKind.TOOL, "Known"),),
            edges=(ActivityEdge("known", "missing"),),
        )
    with pytest.raises(TypeError, match="ActivityTopology"):
        ActivityStartedEvent(topology={"nodes": []})  # type: ignore[arg-type]


def test_product_wire_model_rejects_wrong_activity_shapes() -> None:
    with pytest.raises(ValidationError):
        ActivityStarted.model_validate(
            {
                "activity_kind": "graph",
                "topology": {"nodes": [{"node_id": "node", "kind": "unknown", "label": "Node"}], "edges": []},
            }
        )
    with pytest.raises(ValidationError):
        ActivityCompleted.model_validate(
            {
                "outcome": "unknown",
                "node_states": [],
            }
        )
