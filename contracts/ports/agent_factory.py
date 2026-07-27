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

from typing import Any, Protocol, TypeVar

AgentT = TypeVar("AgentT")


class AgentFactory(Protocol):
    """Construct an Agent class through an application composition root."""

    def build(self, agent_cls: type[AgentT], /, **kwargs: Any) -> AgentT:
        """Return an unstarted, unprovisioned Agent instance."""
        ...


__all__ = ["AgentFactory"]
