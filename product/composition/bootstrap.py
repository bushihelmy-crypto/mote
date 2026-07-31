"""Construction helpers for the Interface-independent Product object graph."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from mote.product.composition.application import Application
from mote.product.composition.container import ProductContainer
from mote.runtime.engine import ClosableAgent, EngineAgentRequest
from mote.runtime.services import EngineServices

AgentT = TypeVar("AgentT", bound=ClosableAgent)


def build_application(
    *,
    container: ProductContainer,
    services: EngineServices,
    agent_factory: Callable[[EngineAgentRequest], AgentT],
) -> Application[AgentT]:
    return Application(
        container=container,
        services=services,
        agent_factory=agent_factory,
    )


__all__ = ["build_application"]
