"""Unique Product composition seam for one resident root Agent."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from mote.contracts.agent import RunnableAgent
from mote.contracts.agent.budget import AgentBudgetPolicy
from mote.contracts.ports.agent.budget import AgentBudgetPort
from mote.contracts.ports.workflow.governance import WorkflowGovernanceCompositionPort
from mote.orchestration.agents.control import AgentControl
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.orchestration.agents.residency.store import ResidencyStore
from mote.product.config.agents import AgentGovernanceConfig
from mote.runtime.control.leases import FileLeaseCoordinator
from mote.runtime.persistence import DiskWriter

OutputT = TypeVar("OutputT")


def compose_resident_agent(
    role: RunnableAgent[OutputT],
    *,
    residency_dir: Path,
    sessions_dir: Path,
    writer: DiskWriter,
    governance: AgentGovernanceConfig,
    budget: AgentBudgetPort,
    workflow_governance: WorkflowGovernanceCompositionPort | None,
) -> tuple[AgentControl, AgentRuntime[OutputT]]:
    """Create, register and bind the sole control plane for a root Agent."""

    residency_leases = FileLeaseCoordinator(residency_dir / "residency-leases.json")
    control = AgentControl(
        session_id=role.session_id,
        store=ResidencyStore(
            base_dir=str(residency_dir),
            sessions_base_dir=str(sessions_dir),
            lease_coordinator=residency_leases,
            writer=writer,
        ),
        residency_lease_coordinator=residency_leases,
        lineage_path=residency_dir / "agent-lineage.json",
        max_logical_agents=governance.logical_agents,
        max_resident_incarnations=governance.resident_incarnations,
        max_concurrent_turns=governance.concurrent_turns,
        turn_queue_capacity=governance.turn_queue_capacity,
        root_turn_weights=tuple(sorted(governance.root_weights.items())),
        budget=budget,
        budget_policy=AgentBudgetPolicy(
            governance.root_token_budget,
            governance.root_cost_micro_usd_budget,
            governance.max_depth,
            frozenset({"delegate"}),
        ),
        child_token_reservation=governance.child_token_reservation,
        child_cost_micro_usd_reservation=governance.child_cost_micro_usd_reservation,
        workflow_governance=workflow_governance,
    )
    runtime = AgentRuntime(role)
    control.add_agent(runtime, root=True)
    return control, runtime


__all__ = ["compose_resident_agent"]
