"""Consumer registration primitives with no discovery side effects."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from mote.cli.contracts.interface import Consumer
from mote.cli.contracts.view import Capabilities


@dataclass(frozen=True)
class ConsumerSpec:
    name: str
    builder: Callable[[Any], Consumer]
    capabilities: Capabilities
    validate: Optional[Callable[[Any], None]] = None


_REGISTRY: Dict[str, ConsumerSpec] = {}


def register_consumer(
    name: str,
    *,
    capabilities: Capabilities,
    validate: Optional[Callable[[Any], None]] = None,
) -> Callable[[Callable[[Any], Consumer]], Callable[[Any], Consumer]]:
    """Register a consumer builder without triggering plugin discovery."""

    def decorate(builder: Callable[[Any], Consumer]) -> Callable[[Any], Consumer]:
        _REGISTRY[name] = ConsumerSpec(name, builder, capabilities, validate)
        return builder

    return decorate


def registered_consumers() -> List[str]:
    return sorted(_REGISTRY)


def consumer_spec(name: str) -> ConsumerSpec | None:
    return _REGISTRY.get(name)


__all__ = ["ConsumerSpec", "consumer_spec", "register_consumer", "registered_consumers"]
