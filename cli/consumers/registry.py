#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``@register_consumer`` + ``build_consumers`` — self-registering host channels.

A new delivery channel is a **new module + one decorator**, zero core changes
(ARCHITECTURE §2.7). Each builder registers under a name with its declared
:class:`Capabilities` and an optional config validator; ``build_consumers(config)``
instantiates whichever channels the config activates — possibly several at once
(terminal + web mirror + machine), which is exactly the §4.4 "stacked hosts" case.

The registry is process-global and populated by import side effect; importing a
``consumers.<channel>`` module registers its builder. ``build_consumers`` imports
the built-in channels lazily (so a missing optional dep in one channel never
breaks the others) before reading the active list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from mote.cli.contracts.interface import Consumer
from mote.cli.contracts.view import Capabilities


@dataclass(frozen=True)
class ConsumerSpec:
    """A registered channel: how to build it + what it can render."""

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
    """Decorator: register a consumer builder under ``name``.

    The decorated function takes the app config and returns a :class:`Consumer`.
    ``capabilities`` are attached to the built consumer (the projector reads them
    to drive downgrade); ``validate`` (optional) may raise on a bad config.
    """

    def _decorator(builder: Callable[[Any], Consumer]) -> Callable[[Any], Consumer]:
        _REGISTRY[name] = ConsumerSpec(name=name, builder=builder, capabilities=capabilities, validate=validate)
        return builder

    return _decorator


def registered_consumers() -> List[str]:
    """Names of all currently-registered channels (for diagnostics/help)."""
    return sorted(_REGISTRY)


def _ensure_builtins_imported() -> None:
    """Import built-in channels so their ``@register_consumer`` runs.

    Each import is isolated: a channel whose optional dependency is missing
    raises only on *its own* import, never blocking the others.
    """
    for module in (
        "mote.cli.consumers.terminal.consumer",
        "mote.cli.consumers.structured.consumer",
    ):
        try:
            __import__(module)
        except Exception:  # noqa: BLE001 — a broken optional channel must not break the rest
            continue


def build_consumer(name: str, config: Any = None) -> Consumer:
    """Build a single registered consumer by name (validating config first)."""
    _ensure_builtins_imported()
    spec = _REGISTRY.get(name)
    if spec is None:
        raise KeyError(f"unknown consumer {name!r}; registered: {registered_consumers()}")
    if spec.validate is not None:
        spec.validate(config)
    consumer = spec.builder(config)
    return consumer


def build_consumers(config: Any = None, *, active: Optional[List[str]] = None) -> List[Consumer]:
    """Instantiate the active set of consumers.

    ``active`` is the list of channel names to build; when ``None`` it is read
    from ``config.consumers`` (falling back to ``["terminal"]`` for the default
    interactive run). Channels that fail to build are skipped (graceful
    degradation, §9.10) so one misconfigured channel never sinks the app.
    """
    _ensure_builtins_imported()
    if active is None:
        active = list(getattr(config, "consumers", None) or ["terminal"])
    out: List[Consumer] = []
    for name in active:
        try:
            out.append(build_consumer(name, config))
        except Exception:  # noqa: BLE001 — graceful degradation per channel
            continue
    return out


__all__ = [
    "ConsumerSpec",
    "register_consumer",
    "registered_consumers",
    "build_consumer",
    "build_consumers",
]
