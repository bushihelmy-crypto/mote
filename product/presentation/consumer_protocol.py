#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``Consumer`` — one host's delivery adapter for a single projected protocol.

A consumer is a structural handler that consumes a
*projected* event (``ViewEvent`` for human hosts, ``ServerNotification`` for
machine hosts) and **delivers** it — to a terminal, a websocket, a Lark card, a
JSON-lines stdout. It can also **eat** an event it can't render: a consumer
never has to handle every event kind.

This is a LEAF interface module: at runtime it imports only ``typing`` (the
``Capabilities`` reference is a ``TYPE_CHECKING``-only annotation), so it can be
imported from any host without risking a cycle. The eat/dispatch *plumbing* that
implements this contract lives separately in
:class:`mote.product.presentation.consumer.BaseConsumer`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from mote.product.presentation.events.capabilities import Capabilities


EventT_contra = TypeVar("EventT_contra", contravariant=True)


@runtime_checkable
class Consumer(Protocol[EventT_contra]):
    """One host's delivery adapter for a single projected protocol.

    ``capabilities`` drives the per-consumer ``CapabilityAdapter`` upstream
    (a non-streaming consumer never sees raw deltas). ``handle`` delivers (or
    eats) one event; ``aclose`` releases any transport the consumer holds.
    """

    capabilities: "Capabilities"

    async def handle(self, ev: EventT_contra) -> None:
        ...

    async def aclose(self) -> None:
        ...


@runtime_checkable
class SyncConsumer(Protocol[EventT_contra]):
    """Explicit synchronous delivery capability."""

    def handle_sync(self, ev: EventT_contra) -> None:
        ...


__all__ = ["Consumer", "SyncConsumer"]
