from types import SimpleNamespace

import pytest

from mote.contracts.agent import SpawnContext
from mote.product.toolsets.builtin.agent_tool import Agent
from mote.runtime.agent.control import set_control


class _AgentDefinition:
    pass


class _SpawnedAgent:
    command_channel = SimpleNamespace(lower=lambda text: text)
    config = SimpleNamespace(router=SimpleNamespace(spawn_routing=False))
    router = SimpleNamespace(strategy=None)


class _ChildHandle:
    def __init__(self, spec):
        self.runtime = SimpleNamespace(role=spec.role_factory(SpawnContext(parent_id=spec.parent_id)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def run_to_completion(self, _message):
        return SimpleNamespace(output="child result")


class _AgentControl:
    def __init__(self):
        self.specs = []

    async def spawn_agent(self, spec):
        self.specs.append(spec)
        return _ChildHandle(spec)


@pytest.mark.asyncio
async def test_agent_tool_builds_child_through_injected_capability():
    catalog = SimpleNamespace(
        get=lambda _name: _AgentDefinition,
        all_agents=lambda: {"worker": _AgentDefinition},
    )
    calls = []

    def build_child_agent(agent_cls, /, **kwargs):
        calls.append((agent_cls, kwargs))
        return _SpawnedAgent()

    role = SimpleNamespace(tool_capabilities=lambda: {"build_child_agent": build_child_agent})
    tool = Agent(catalog).bind("parent-session", role)
    control = _AgentControl()

    with set_control(control):
        result = await tool.call(agent_type="worker", prompt="inspect the change")

    assert result == "child result"
    assert calls == [(_AgentDefinition, {"parent_session_id": "parent-session"})]
    assert len(control.specs) == 1
