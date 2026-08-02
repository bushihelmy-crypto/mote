"""Product-owned routing policies and composition-root bindings."""

from __future__ import annotations

from collections.abc import Callable

from mote.contracts.ports.model.routing import RoutingPolicy
from mote.product.routing.squilla.ml.runtime import RoutingModelRuntime
from mote.product.routing.squilla.strategy import SquillaStrategy


def builtin_routing_strategy_builders(
    runtime: RoutingModelRuntime,
) -> dict[str, Callable[[], RoutingPolicy]]:
    return {"squilla": lambda: SquillaStrategy(runtime)}


__all__ = ["builtin_routing_strategy_builders"]
