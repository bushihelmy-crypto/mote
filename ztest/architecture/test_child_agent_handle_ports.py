from __future__ import annotations

from pathlib import Path
from typing import get_type_hints

from mote.contracts.agent import RunnableAgent
from mote.contracts.ports.agent.control import ChildReleasePort, ResidencyReservationPort
from mote.orchestration.agents.lifecycle.handle import ChildAgentHandle

ROOT = Path(__file__).resolve().parents[2]


def test_child_teardown_ports_are_minimal() -> None:
    assert set(ChildReleasePort.__dict__) & {"release_child"} == {"release_child"}
    assert set(ResidencyReservationPort.__dict__) & {"rollback"} == {"rollback"}


def test_child_handle_has_no_unbounded_teardown_types() -> None:
    source = (ROOT / "orchestration/agents/lifecycle/handle.py").read_text(encoding="utf-8")
    assert "control: Any" not in source
    assert "residency_slot: Optional[Any]" not in source
    assert "residency_slot: Any" not in source
    assert "ChildReleasePort" in source
    assert "ResidencyReservationPort" in source


def test_child_handle_preserves_typed_runnable_agent_view() -> None:
    assert get_type_hints(ChildAgentHandle.agent.fget)["return"] == RunnableAgent[ChildAgentHandle.__parameters__[0]]
