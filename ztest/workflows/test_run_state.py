"""Unit tests for process-local graph activity state."""

from mote.orchestration.workflows import END, START, GraphState, Stage, WorkflowBuilder
from mote.orchestration.workflows.types import GraphRunState, NodeRecord, WorkflowNodeStatus


class SimpleState(GraphState):
    x: int = 0


def _node(field: str):
    async def node(state):
        async def submit():
            return {field: state.x}

        return Stage(submit=submit())

    return node


def _graph():
    graph = WorkflowBuilder("state", state_schema=SimpleState)
    graph.add_node("a", _node("a"))
    graph.add_node("b", _node("b"))
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    return graph


def test_for_graph_seeds_pending_records() -> None:
    state = GraphRunState.for_graph(_graph())
    assert set(state.records) == {"a", "b"}
    assert all(record.status is WorkflowNodeStatus.PENDING for record in state.records.values())
    assert state.activity_execution_id


def test_transitions_preserve_attempt_count_and_terminal_names() -> None:
    state = GraphRunState(records={"a": NodeRecord(name="a"), "b": NodeRecord(name="b")})
    state.mark_running("a")
    state.mark_failed("a", ValueError("boom details"))
    state.reset("a")
    state.mark_running("a")
    state.mark_success("a")
    state.mark_skipped("b")
    assert state.records["a"].attempts == 2
    assert state.records["a"].last_error is None
    assert state.completed_names() == {"a", "b"}


def test_inference_does_not_guess_completion_from_state_fields() -> None:
    graph = _graph()
    snapshot = SimpleState(x=5)
    setattr(snapshot, "a", 10)
    state = GraphRunState.infer_from_state(graph, snapshot)
    assert state.completed_names() == set()
