"""Layer-neutral Agent construction port.

The Runtime owns the point where a child Agent is requested, while the Product
owns the concrete composition root that knows which tools, routing strategies,
and background services belong to that Agent.  This protocol keeps that
dependency flowing downward: Runtime stores and invokes the port without ever
importing a Product factory.

Construction is deliberately separate from spawn policy.  Implementations only
create an unstarted Agent; AgentControl remains the sole authority for admission,
lineage, context provisioning, execution, and teardown.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from mote.contracts.agent import AgentBuilder, AgentConstructionRequest

AgentT = TypeVar("AgentT")


class AgentFactory(Protocol):
    """Bind a Product-private Agent declaration to its child builder."""

    def child_builder(self, agent_cls: object, /) -> AgentBuilder[AgentConstructionRequest, str]:
        """Return a builder with the concrete Agent class already bound."""
        ...


__all__ = ["AgentFactory"]
