from __future__ import annotations

import pytest

from mote.product.cli.consumers.core import ConsumerRegistry, ConsumerSpec
from mote.product.cli.contracts.view import Capabilities


def _spec(name: str, builder=lambda config: config) -> ConsumerSpec:
    return ConsumerSpec(name=name, builder=builder, capabilities=Capabilities())


def test_consumer_registries_are_isolated() -> None:
    first = ConsumerRegistry()
    second = ConsumerRegistry()
    first.register(_spec("terminal"))

    assert first.names() == ["terminal"]
    assert second.names() == []


def test_consumer_registry_rejects_conflicting_name() -> None:
    registry = ConsumerRegistry()
    registry.register(_spec("terminal"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec("terminal", builder=lambda config: None))
