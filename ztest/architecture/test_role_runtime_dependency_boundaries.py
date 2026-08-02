from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mote.kernel.output import text_output_contract
from mote.product.agents.factory import CodingAgentFactory
from mote.runtime.agent.role import Role
from mote.runtime.agent.role_state import RoleState
from mote.runtime.agent.wiring import AgentWiring


class _Human:
    def __init__(self, identity: str) -> None:
        self.identity = identity


def test_role_state_has_no_runtime_environment_field() -> None:
    assert "env" not in RoleState.model_fields


def test_retired_environment_facades_are_absent() -> None:
    root = Path(__file__).parents[2]
    assert not (root / "orchestration/agents/base_environment.py").exists()
    assert not (root / "orchestration/agents/environment_facade.py").exists()
    assert not (root / "product/interaction/mote_env.py").exists()


@pytest.mark.asyncio
async def test_human_binding_is_task_scoped() -> None:
    role = Role(
        name="scoped",
        wiring=AgentWiring.for_dependencies(
            CodingAgentFactory().dependencies(deps=None, output_contract=text_output_contract())
        ),
    )
    ready = asyncio.Event()
    release = asyncio.Event()

    async def connection(identity: str) -> str:
        human: Any = _Human(identity)
        token = role.bind_human_interaction(human)
        ready.set()
        await release.wait()
        observed = role.human_interaction
        role.reset_human_interaction(token)
        assert observed is human
        return identity

    first = asyncio.create_task(connection("first"))
    await ready.wait()
    second = asyncio.create_task(connection("second"))
    await asyncio.sleep(0)
    assert role.human_interaction is None
    release.set()
    assert await asyncio.gather(first, second) == ["first", "second"]
    assert role.human_interaction is None
