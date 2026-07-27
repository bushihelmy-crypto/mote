from mote.contracts.think import ThinkResult
from mote.kernel import AgentRunState, AgentSpec
from mote.runtime.agent import RoleSchema


def test_agent_spec_owns_only_kernel_agent_definition():
    assert set(AgentSpec.model_fields) == {
        "name",
        "profile",
        "system_prompt",
        "cmd_prompt",
        "role_info",
        "command_protocol",
        "max_cost",
        "think_kind",
        "max_auto_continue",
        "deferred_tools",
        "tools",
        "mcps",
        "agents",
        "skills",
        "enable_memory",
        "observe_all_msg_from_buffer",
    }
    assert "permissions" not in AgentSpec.model_fields
    assert "record_checkpoints" not in AgentSpec.model_fields
    assert "browser_headless" not in AgentSpec.model_fields


def test_role_schema_extends_agent_spec_without_nested_wire_shape():
    schema = RoleSchema(name="Reviewer", tools=["Read"], record_checkpoints=False)

    assert isinstance(schema, AgentSpec)
    assert schema.model_dump()["name"] == "Reviewer"
    assert schema.model_dump()["tools"] == ["Read"]
    assert "agent_spec" not in schema.model_dump()
    assert schema.record_checkpoints is False


def test_agent_spec_collection_defaults_are_isolated():
    first = AgentSpec()
    second = AgentSpec()

    first.tools.append("Custom")
    assert "Custom" not in second.tools


def test_canvas_is_equipped_and_deferred_by_default():
    spec = AgentSpec()

    assert "Canvas" in spec.tools
    assert "Canvas" in spec.deferred_tools


def test_handoff_is_exposed_by_stateful_tools_instead_of_a_standalone_tool():
    spec = AgentSpec()

    assert "Handoff" not in spec.tools
    assert "Handoff" not in spec.deferred_tools


def test_agent_run_state_is_transient_kernel_state():
    state = AgentRunState()

    assert state.active is False
    assert isinstance(state.last_think_result, ThinkResult)
