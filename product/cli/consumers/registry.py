#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Explicit construction and use of isolated Product consumer catalogs.

A new delivery channel is a **new module + one decorator**, zero core changes
(ARCHITECTURE §2.7). Each builder registers under a name with its declared
:class:`Capabilities` and an optional config validator; ``build_consumers(config)``
instantiates whichever channels the config activates — possibly several at once
(terminal + web mirror + machine), which is exactly the §4.4 "stacked hosts" case.

The Product owns immutable builtin definitions and copies them into each
Application's registry. Optional rendering dependencies are guarded at their
module import boundaries, so catalog construction itself stays deterministic.
"""

from __future__ import annotations

from typing import Any, List, Optional

from mote.product.cli.consumers.core import ConsumerRegistry, ConsumerSpec, consumer_definition, register_consumer
from mote.product.cli.consumers.structured.consumer import build_structured_consumer
from mote.product.cli.consumers.terminal.consumer import build_terminal_consumer
from mote.product.cli.contracts.interface import Consumer

BUILTIN_CONSUMERS: tuple[ConsumerSpec, ...] = (
    consumer_definition(build_terminal_consumer),
    consumer_definition(build_structured_consumer),
)


def default_registry() -> ConsumerRegistry:
    registry = ConsumerRegistry()
    for spec in BUILTIN_CONSUMERS:
        registry.register(spec)
    return registry


def registered_consumers(*, registry: ConsumerRegistry | None = None) -> List[str]:
    """List one catalog's consumer names, using a fresh builtin catalog by default."""

    return (registry or default_registry()).names()


def build_consumer(
    name: str,
    config: Any = None,
    *,
    registry: ConsumerRegistry | None = None,
) -> Consumer:
    """Build a single registered consumer by name (validating config first)."""
    return (registry or default_registry()).build(name, config)


def build_consumers(
    config: Any = None,
    *,
    active: Optional[List[str]] = None,
    registry: ConsumerRegistry | None = None,
) -> List[Consumer]:
    """Instantiate the active set of consumers.

    ``active`` is the list of channel names to build; when ``None`` it is read
    from ``config.consumers`` (falling back to ``["terminal"]`` for the default
    interactive run). Channels that fail to build are skipped (graceful
    degradation, §9.10) so one misconfigured channel never sinks the app.
    """
    catalog = registry or default_registry()
    if active is None:
        active = list(getattr(config, "consumers", None) or ["terminal"])
    out: List[Consumer] = []
    for name in active:
        try:
            out.append(catalog.build(name, config))
        except Exception:  # noqa: BLE001 — graceful degradation per channel
            continue
    return out


__all__ = [
    "ConsumerSpec",
    "ConsumerRegistry",
    "register_consumer",
    "registered_consumers",
    "default_registry",
    "build_consumer",
    "build_consumers",
]
