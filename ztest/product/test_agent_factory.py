import pytest

from mote.kernel.output import text_output_contract
from mote.product.agents import CodingAgentFactory
from mote.runtime.agent import AgentDependencies, AgentWiring


class _Agent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_factory_injects_product_dependencies():
    toolsets = (object(),)
    pool_builder = object()
    routing_builder = object()
    factory = CodingAgentFactory(
        toolsets_factory=lambda _protocol: toolsets,
        background_task_pool_builder=pool_builder,
        routing_strategy_builders_factory=lambda: {"tenant": routing_builder},
    )

    agent = factory.build(_Agent, name="worker")

    assert agent.kwargs["name"] == "worker"
    wiring = agent.kwargs["wiring"]
    assert wiring.dependencies.toolsets == toolsets
    assert wiring.dependencies.agent_factory is factory
    assert wiring.dependencies.background_task_pool_builder is pool_builder
    assert wiring.dependencies.routing_strategy_builders == {"tenant": routing_builder}


def test_explicit_dependencies_override_product_defaults():
    factory = CodingAgentFactory(toolsets_factory=lambda _protocol: ("default",))

    wiring = AgentWiring(
        dependencies=AgentDependencies(
            deps=None,
            output_contract=text_output_contract(),
            toolsets=("custom",),
            background_task_pool_builder="custom-pool",
            routing_strategy_builders={"custom": object()},
        )
    )
    agent = factory.build(_Agent, wiring=wiring)

    assert agent.kwargs["wiring"] is wiring


def test_explicit_wiring_rejects_parallel_context_argument():
    factory = CodingAgentFactory()

    with pytest.raises(ValueError, match="mutually exclusive"):
        factory.build(_Agent, wiring=AgentWiring.defaults(), services=object())
