"""SpawnGate — the agent-spawn depth limit as a control-plane subscriber.

The spawn-depth veto used to be a *direct call* wedged into
``AgentControl.spawn_agent``::

    if max_depth is not None and exceeds_agent_spawn_depth_limit(child_depth, max_depth):
        raise AgentLimitReached(...)

That made it a hidden vetoer the runtime bus knew nothing about — exactly the
shape the permission engine had before it moved onto the plane. This subscriber
puts the gate *on* the control plane so it is a first-class, ordered, foldable
influence: a new spawn policy (a cost ceiling, a per-role quota, a business-hours
guard) is added by registering another control subscriber, not by threading more
``if`` branches through the birth channel.

* ``fail_mode = FAIL_CLOSED`` makes a crash/timeout in the gate **deny** the
  spawn rather than let an unbounded child through — a limit that "could not
  decide" must fail safe.

The gate is pure: it reads only the resolved lineage facts the
:class:`~metagpt.common.events.types.PreAgentSpawnEvent` carries (parent path,
child depth, effective ceiling), so it imports no runtime state. The emitter
(``AgentControl.spawn_agent``) translates a ``deny`` outcome back into the
``AgentLimitReached`` its callers already expect.
"""
from __future__ import annotations

from typing import Optional

from metagpt.common.events.outcomes import SpawnOutcome
from metagpt.common.events.types import PRE_AGENT_SPAWN, PreAgentSpawnEvent
from metagpt.common.interface.event_subscriber import FAIL_CLOSED, ControlStage
from metagpt.environment.registry import exceeds_agent_spawn_depth_limit


class SpawnGate:
    """Deny a child spawn that would exceed the configured depth ceiling."""

    #: Only spawn-admission events reach this subscriber (bus routing key).
    handles: tuple[str, ...] = (PRE_AGENT_SPAWN,)
    #: Gate stage — the final say on whether the child is admitted.
    stage: ControlStage = ControlStage.GATE
    #: A gate that cannot evaluate must deny, not wave the spawn through.
    fail_mode: str = FAIL_CLOSED
    #: Provenance label (declared for consistency; a deny rewrites no field).
    name: str = "spawn_gate"

    async def handle_control(self, event) -> Optional[SpawnOutcome]:
        # Only spawn requests are gated; everything else is not ours to judge.
        if not isinstance(event, PreAgentSpawnEvent):
            return None
        if event.max_depth is not None and exceeds_agent_spawn_depth_limit(
            event.child_depth, event.max_depth
        ):
            return SpawnOutcome(
                denied=True,
                reason=f"spawn depth limit ({event.max_depth}) reached at {event.parent_path}",
            )
        return None

    @staticmethod
    def on_failure(reason: str) -> SpawnOutcome:
        """Typed deny the bus folds when this gate itself crashes/times out."""
        return SpawnOutcome(denied=True, reason=reason)


__all__ = ["SpawnGate"]
