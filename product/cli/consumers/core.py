"""Consumer registration primitives with no discovery side effects."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, cast

from mote.product.cli.contracts.interface import Consumer
from mote.product.cli.contracts.view import Capabilities


@dataclass(frozen=True)
class ConsumerSpec:
    name: str
    builder: Callable[[Any], Consumer]
    capabilities: Capabilities
    validate: Optional[Callable[[Any], None]] = None


class ConsumerRegistry:
    """Isolated catalog of named consumer builders."""

    def __init__(self) -> None:
        self._specs: dict[str, ConsumerSpec] = {}

    def register(self, spec: ConsumerSpec) -> None:
        existing = self._specs.get(spec.name)
        if existing is not None and existing != spec:
            raise ValueError(f"Consumer {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def names(self) -> List[str]:
        return sorted(self._specs)

    def get(self, name: str) -> ConsumerSpec | None:
        return self._specs.get(name)

    def build(self, name: str, config: Any = None) -> Consumer:
        spec = self.get(name)
        if spec is None:
            raise KeyError(f"unknown consumer {name!r}; registered: {self.names()}")
        if spec.validate is not None:
            spec.validate(config)
        return spec.builder(config)


_CONSUMER_DEFINITION_ATTR = "__mote_consumer_definition__"


def register_consumer(
    name: str,
    *,
    capabilities: Capabilities,
    validate: Optional[Callable[[Any], None]] = None,
) -> Callable[[Callable[[Any], Consumer]], Callable[[Any], Consumer]]:
    """Declare consumer metadata without mutating a process-global catalog."""

    def decorate(builder: Callable[[Any], Consumer]) -> Callable[[Any], Consumer]:
        setattr(
            builder,
            _CONSUMER_DEFINITION_ATTR,
            ConsumerSpec(name, builder, capabilities, validate),
        )
        return builder

    return decorate


def consumer_definition(builder: Callable[[Any], Consumer]) -> ConsumerSpec:
    definition = getattr(builder, _CONSUMER_DEFINITION_ATTR, None)
    if not isinstance(definition, ConsumerSpec):
        raise TypeError(f"{builder!r} is not decorated with @register_consumer")
    return cast(ConsumerSpec, definition)


__all__ = [
    "ConsumerRegistry",
    "ConsumerSpec",
    "consumer_definition",
    "register_consumer",
]
