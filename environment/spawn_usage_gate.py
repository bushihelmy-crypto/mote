"""SpawnUsageGate — a tree-wide token/cost ceiling on the control plane.

The fleet's single *cost* ceiling: it caps *how much the whole fleet may spend*
— mote's analogue of pydantic-ai-harness ``DynamicWorkflow``'s
``sub_agent_usage_limits`` (a tree-wide token cap) and its cost dimension. It
complements the orthogonal spawn ceilings — depth (``SpawnGate``),
live-incarnation count (``max_agents`` / residency), and, for a single
declarative orchestration, total node activations (run_graph's
``recursion_limit``). Spend is the meaningful budget; a cumulative spawn-*count*
ceiling would only duplicate these (count is a poor proxy for cost).

It reads the fleet's **live** cumulative spend from the cost mirror tree
(:class:`~mote.router.cost.node.CostNode`) that ``AgentControl`` already
maintains — one node per agent, ``subtree_cost()`` / ``subtree_usage()`` rolling
up the lineage on demand. Because that spend is fleet-global state, not a
per-spawn lineage fact, it does **not** ride the ``PreAgentSpawnEvent`` (which
carries only lineage facts); instead the gate takes injected *reader closures*
(mirroring how :class:`~mote.environment.residency.Residency` takes injected
callbacks), keeping the gate decoupled from the cost layer.

Scope & semantics
-----------------
The check fires at **spawn admission**: before a new child is born, if the fleet
has already spent its token or cost budget, the spawn is denied. This is the
runaway-fan-out guard's cost dimension — it refuses *new* fan-out once the tree
is over budget. It is deliberately **best-effort and tree-wide**, matching
``DynamicWorkflow``'s own note that a shared-counter cap is "best-effort under
concurrent fan-out": it does not interrupt an already-running agent mid-turn
(that is the LLM loop's / ``RecoveryRunner``'s concern), and each in-flight turn
may overshoot by its final response before the next spawn is refused.

Ordering
--------
Sits in the shared ``ControlStage.GATE`` bucket. ``AgentControl`` subscribes it
*after* ``SpawnGate`` so a depth-denied spawn short-circuits first (a cheap
lineage veto before this gate reads the live cost tree). Both gates are
read-only, so their relative order is otherwise immaterial.

* ``fail_mode = FAIL_CLOSED`` — a gate that cannot decide must deny, never wave an
  over-budget child through.
"""
from __future__ import annotations

from typing import Callable, ClassVar, Optional

from mote.common.events.outcomes import SpawnOutcome
from mote.common.events.types import PRE_AGENT_SPAWN, PreAgentSpawnEvent
from mote.common.interface.event_subscriber import FAIL_CLOSED, ControlStage, ControlSubscriber, FailMode


class SpawnUsageGate(ControlSubscriber):
    """Deny a child spawn once the fleet's token or cost budget is spent."""

    #: Only spawn-admission events reach this subscriber (bus routing key).
    handles: ClassVar[tuple[str, ...]] = (PRE_AGENT_SPAWN,)
    #: Gate stage — a final say on whether the child is admitted.
    stage: ClassVar[ControlStage] = ControlStage.GATE
    #: A gate that cannot evaluate must deny, not wave the spawn through.
    fail_mode: ClassVar[FailMode] = FAIL_CLOSED
    #: Provenance label (declared for consistency; a deny rewrites no field).
    name: ClassVar[str] = "spawn_usage_gate"

    def __init__(
        self,
        *,
        max_cost_usd: Optional[float] = None,
        max_total_tokens: Optional[int] = None,
        cost_reader: Optional[Callable[[], float]] = None,
        tokens_reader: Optional[Callable[[], int]] = None,
    ) -> None:
        #: Fleet USD-cost ceiling; ``None`` == no cost cap.
        self._max_cost_usd = max_cost_usd
        #: Fleet total-token ceiling; ``None`` == no token cap.
        self._max_total_tokens = max_total_tokens
        #: Reads the fleet's live cumulative USD spend (lazy — the cost tree may
        #: not exist until the root agent is added).
        self._cost_reader = cost_reader
        #: Reads the fleet's live cumulative total-token count.
        self._tokens_reader = tokens_reader

    async def handle_control(self, event) -> Optional[SpawnOutcome]:
        # Only spawn requests are gated; everything else is not ours to judge.
        if not isinstance(event, PreAgentSpawnEvent):
            return None
        # Cost ceiling — a cap with no reader is inert (nothing to measure).
        if self._max_cost_usd is not None and self._cost_reader is not None:
            spent = self._cost_reader()
            if spent >= self._max_cost_usd:
                return SpawnOutcome(
                    denied=True,
                    reason=f"fleet cost budget (${self._max_cost_usd:.2f}) reached (${spent:.2f} spent)",
                )
        # Token ceiling.
        if self._max_total_tokens is not None and self._tokens_reader is not None:
            used = self._tokens_reader()
            if used >= self._max_total_tokens:
                return SpawnOutcome(
                    denied=True,
                    reason=f"fleet token budget ({self._max_total_tokens}) reached ({used} used)",
                )
        return None

    @staticmethod
    def on_failure(reason: str) -> SpawnOutcome:
        """Typed deny the bus folds when this gate itself crashes/times out."""
        return SpawnOutcome(denied=True, reason=reason)


__all__ = ["SpawnUsageGate"]
