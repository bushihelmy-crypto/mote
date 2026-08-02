import pytest

from mote.contracts.agent import AgentConstructionRequest, BaseAgent, ContextPolicy, SpawnContext
from mote.kernel.output import text_output_contract
from mote.product.agents import CodingAgentFactory, RootAgentRequest
from mote.runtime.agent import AgentDependencies, AgentWiring, Role
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.agent.role_state import RoleState
from mote.runtime.tools.provider import NativeToolset


def _native_tools():
    return NativeToolset(id="test", definitions=())


def test_root_builder_injects_explicit_product_dependencies():
    toolsets = (_native_tools(),)
    pool_builder = object()
    routing_policy = object()
    routing_builder = lambda: routing_policy
    factory = CodingAgentFactory(
        toolsets_factory=lambda _protocol: toolsets,
        background_task_pool_builder=pool_builder,
        routing_strategy_builders_factory=lambda: {"tenant": routing_builder},
    )
    schema = RoleSchema(name="worker")
    dependencies = factory.dependencies(
        deps=None,
        output_contract=text_output_contract(),
        command_protocol=schema.command_protocol,
    )

    agent = factory.root_builder(Role).build(
        RootAgentRequest(
            name="worker",
            role_schema=schema,
            state=RoleState(),
            wiring=AgentWiring(dependencies=dependencies),
        )
    )

    projection = agent.wiring.dependencies.component_projection
    assert projection is not None
    assert projection.action().toolsets == toolsets
    assert projection.action().background_task_pool_builder is pool_builder
    assert projection.cognition().routing_strategy_factory.build("tenant") is routing_policy


def test_root_builder_preserves_explicit_wiring():
    factory = CodingAgentFactory(toolsets_factory=lambda _protocol: (_native_tools(),))
    wiring = AgentWiring(
        dependencies=factory.dependencies(
            deps=None,
            output_contract=text_output_contract(),
            toolsets=(_native_tools(),),
        )
    )

    agent = factory.root_builder(Role).build(
        RootAgentRequest(
            role_schema=RoleSchema(),
            state=RoleState(),
            wiring=wiring,
        )
    )

    assert agent.wiring is wiring


def test_child_builder_rejects_non_runnable_agent_result():
    class _DeclaredAgent(BaseAgent):
        def __init__(self, **_kwargs):
            pass

    factory = CodingAgentFactory()
    request = AgentConstructionRequest(
        logical_agent_id="child-agent",
        parent_session_id=None,
        child_identity="child",
        child_path="root/child",
        nickname="child",
        cwd=None,
        context_policy=ContextPolicy.FRESH,
        spawn_context=SpawnContext(),
    )

    with pytest.raises(TypeError, match="non-runnable"):
        factory.child_builder(_DeclaredAgent).build(request)
