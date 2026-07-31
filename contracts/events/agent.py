"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    pass

AGENT_LIFECYCLE = "agent_lifecycle"

BUDGET = "budget"


@dataclass
class BudgetEvent:
    """An agent crossed a spend threshold against its configured budget cap.

    Emitted by :class:`ContextProvider.enforce_budget` on the observation plane
    (fire-and-forget) when this agent's own accrued spend crosses the soft
    warning line (``stopped=False``, once) or the hard cap (``stopped=True``,
    once). The loop reads the returned verdict to actually halt; this event is
    purely for the UI/recorder to surface + persist the milestone. Only emitted
    when a positive ``max_cost`` is configured — an unbudgeted agent is silent.
    """

    spend: float = 0.0  # USD accrued by this agent so far
    limit: float = 0.0  # configured max_cost cap (USD)
    fraction: float = 0.0  # spend / limit at emit time
    stopped: bool = False  # True once the hard cap halted the loop

    name: ClassVar[str] = BUDGET


@dataclass
class AgentLifecycleEvent:
    """An agent crossed a residency/control-plane boundary.

    The orchestration layer (control / residency) runs outside per-turn Role
    execution, so it owns a persistent TelemetryRuntime for these milestones:
    ``added`` / ``rehydrated`` / ``evicted`` / ``interrupted``.
    """

    session_id: str = ""
    phase: str = ""
    detail: str = ""

    name: ClassVar[str] = AGENT_LIFECYCLE
