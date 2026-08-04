"""Composition Port binding Workflow terminal facts to canonical Agent ingress."""

from typing import Protocol

from mote.contracts.agent.runtime_identity import AgentId
from mote.contracts.ports.agent.delivery import AgentDeliveryPort


class WorkflowAgentDeliveryCompositionPort(Protocol):
    def register_agent_delivery(self, agent_id: AgentId, delivery: AgentDeliveryPort) -> None: ...

    def unregister_agent_delivery(self, agent_id: AgentId, delivery: AgentDeliveryPort) -> None: ...


__all__ = ["WorkflowAgentDeliveryCompositionPort"]
