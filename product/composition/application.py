"""Product lifecycle root pairing one Runtime Engine with its catalogs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from mote.product.composition.container import ProductContainer
from mote.runtime.engine import ClosableAgent, Engine, EngineAgentRequest
from mote.runtime.services import EngineServices

AgentT = TypeVar("AgentT", bound=ClosableAgent)


class Application(Engine[EngineAgentRequest, AgentT], Generic[AgentT]):
    """An Engine whose Product composition remains isolated for its lifetime."""

    def __init__(
        self,
        *,
        container: ProductContainer,
        services: EngineServices,
        agent_factory: Callable[[EngineAgentRequest], AgentT],
    ) -> None:
        super().__init__(services=services, agent_factory=agent_factory)
        self.container = container


__all__ = ["Application"]
