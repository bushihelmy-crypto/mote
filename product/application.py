"""Product lifecycle root pairing one Runtime Engine with its catalogs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from mote.product.container import ProductContainer
from mote.runtime.engine import Engine
from mote.runtime.services import EngineServices

AgentT = TypeVar("AgentT")


class Application(Engine[AgentT], Generic[AgentT]):
    """An Engine whose Product composition remains isolated for its lifetime."""

    def __init__(
        self,
        *,
        container: ProductContainer,
        services: EngineServices,
        agent_factory: Callable[..., AgentT],
    ) -> None:
        super().__init__(services=services, agent_factory=agent_factory)
        self.container = container


__all__ = ["Application"]
