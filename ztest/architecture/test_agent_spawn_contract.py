from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

from mote.contracts.agent import CostAttributionPort, RunnableAgent, SpawnContext

ROOT = Path(__file__).resolve().parents[2]


def test_spawn_context_contains_only_stable_construction_values() -> None:
    assert set(SpawnContext.__annotations__) == {
        "parent_id",
        "agent_path",
        "cwd",
        "parent_session_id",
    }
    assert SpawnContext.__annotations__["agent_path"] == "Optional[str]"


def test_runnable_agent_uses_typed_control_and_cost_ports() -> None:
    assert "bind_agent_control" not in RunnableAgent.__dict__
    assert get_type_hints(RunnableAgent.spawn_cost_attribution)["return"] is CostAttributionPort


def test_spawn_production_boundary_has_no_generic_recovery_seams() -> None:
    sources = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "contracts/agent/spawn.py",
            "orchestration/agents/lifecycle/admission.py",
            "product/agents/factory.py",
        )
    )
    for forbidden in (
        "RunnableAgent[object]",
        "is_text_runnable_agent",
        'getattr(extension, "evaluate")',
        "# type: ignore[attr-defined]",
        "parent_cost_tracker",
    ):
        assert forbidden not in sources
    assert "list[tuple[SpawnPolicyExtensionSpec, SpawnPolicyExtension]]" in sources


def test_product_child_builder_is_statically_text_specialized() -> None:
    source = inspect.getsource(__import__("mote.product.agents.factory", fromlist=["_ChildBuilder"]))
    assert "def build(self, request: AgentConstructionRequest) -> RunnableAgent[str]" in source
    assert "return self.factory.construct_child" in source
    assert "_is_child_agent_class" not in source
    assert "TypeGuard" not in source
    assert "def child_builder(self, agent_cls: object" not in source
