from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import FinalCandidateAction, ModelTurn
from mote.kernel.execution.run_state import AgentRunState
from mote.kernel.execution.state import ExecutionState, NoModelTurn, PendingCandidate
from mote.product.agents.defaults import DEFAULT_DEFERRED_TOOLS, DEFAULT_TOOLS
from mote.runtime.agent import RoleSchema


def test_role_schema_owns_deployment_configuration():
    schema = RoleSchema(name="Reviewer", tools=["Read"], record_checkpoints=False)
    assert schema.model_dump()["name"] == "Reviewer"
    assert schema.model_dump()["tools"] == ["Read"]
    assert schema.record_checkpoints is False


def test_product_owns_coding_agent_tool_defaults():
    assert "Canvas" in DEFAULT_TOOLS
    assert "Canvas" in DEFAULT_DEFERRED_TOOLS
    assert "Skill" in DEFAULT_TOOLS
    assert "Skill" not in DEFAULT_DEFERRED_TOOLS
    assert "Handoff" not in DEFAULT_TOOLS


def test_agent_run_state_is_transient_execution_state():
    state = AgentRunState()
    assert state.active is False
    assert isinstance(state.last_inference_result, InferenceResult)


def test_execution_state_uses_explicit_no_model_turn():
    from mote.contracts.conversation import AIMessage

    state = ExecutionState[str](response=AIMessage(content=""))
    assert isinstance(state.turn, NoModelTurn)


def test_pending_candidate_validates_candidate_index():
    turn = ModelTurn(actions=[FinalCandidateAction(raw="ok", representation="text")])
    assert PendingCandidate(turn, 0).candidate_index == 0

    import pytest

    with pytest.raises(ValueError, match="does not identify"):
        PendingCandidate(turn, 1)
